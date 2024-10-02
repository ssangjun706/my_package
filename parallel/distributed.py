import os
import sys
import getpass
import socket

from setproctitle import *
from multiprocessing import get_context

import torch
import torch.nn as nn
import torch.cuda as cuda
import torch.distributed as dist

from torch.utils.data import DataLoader
from torch.nn.parallel import DistributedDataParallel
from torch.utils.data.distributed import DistributedSampler

from contextlib import contextmanager
from typing import Callable


@contextmanager
def suppress_output():
    with open(os.devnull, "w") as devnull:
        stdout = sys.stdout
        stderr = sys.stderr
        sys.stdout = devnull
        sys.stderr = devnull
        try:
            yield
        finally:
            sys.stderr = stderr
            sys.stdout = stdout


class DistributedDataLoader:
    def __init__(
        self,
        dataloader: DataLoader,
        rank: int,
    ):
        dataset = dataloader.dataset
        batch_size = dataloader.batch_size
        shuffle = isinstance(dataloader.sampler, torch.utils.data.RandomSampler)
        num_workers = dataloader.num_workers
        pin_memory = dataloader.pin_memory
        drop_last = dataloader.drop_last
        device_count = cuda.device_count()

        assert batch_size % device_count == 0, f"(batch size) % (device count) != 0"
        batch_size //= device_count

        sampler = DistributedSampler(
            dataset=dataset,
            shuffle=shuffle,
            drop_last=drop_last,
            num_replicas=device_count,
            rank=rank,
        )
        self.loader = DataLoader(
            dataset=dataset,
            batch_size=batch_size,
            num_workers=num_workers,
            pin_memory=pin_memory,
            sampler=sampler,
        )

    def __iter__(self):
        return iter(self.loader)

    def __len__(self):
        return len(self.loader)
    

class Arguments:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    def set(self, **kwargs):
        for k, v in kwargs.items():
            if hasattr(self, k):
                delattr(self, k)
            setattr(self, k, v)


class DistributedTrainer:
    def __init__(
        self,
        func: Callable,
        model: nn.Module,
        train_loader: DataLoader,
        test_loader: DataLoader,
        epoch: int = 1,
        port: int = 8888,
    ):
        self.port = port
        self.func = func
        self.epoch = epoch
        self.model = model
        self.train_loader = train_loader
        self.test_loader = test_loader
        self.world_size = cuda.device_count()
        self._context = get_context("spawn")
        self._queue = self._context.Queue()
        assert not socket_in_use(port), f"Port {port} is already in use."

    def _runner(self, rank: int):
        setproctitle(f"{getpass.getuser()}/python/parallel/worker")
        torch.cuda.set_device(rank)
        stream = torch.cuda.Stream()
        with torch.cuda.stream(stream):
            model = self.model.to(rank, non_blocking=True)
        
        torch.cuda.current_stream().wait_stream(stream)
        model = DistributedDataParallel(model, device_ids=[rank])
    
        train_loader = DistributedDataLoader(self.train_loader, rank)
        test_loader = DistributedDataLoader(self.test_loader, rank)
        
        train_loader = PrefetchLoader(train_loader)
        test_loader = PrefetchLoader(test_loader)
        
        args = Arguments(
            rank=rank,
            model=model,
            train_loader=train_loader,
            test_loader=test_loader,
        )
        for epoch in range(1, self.epoch + 1):
            args.set(epoch=epoch)
            model.train()
            if rank == 0:
                output = self.func(args)
            else:
                with suppress_output():
                    output = self.func(args)

            if isinstance(output, torch.Tensor):
                if output.is_cuda:
                    output = (float(output.detach().cpu()),)
            elif isinstance(output, tuple):
                output = tuple(
                    (
                        float(e.detach().cpu())
                        if isinstance(e, torch.Tensor) and e.is_cuda
                        else e
                    )
                    for e in output
                )

            if output is not None:
                self._queue.put(output)
            dist.barrier()

    def _worker(self, ngpus_per_node: int, rank: int):
        try:
            dist.init_process_group(
                backend="nccl",
                init_method=f"tcp://localhost:{self.port}",
                world_size=ngpus_per_node,
                rank=rank,
            )
            self._runner(rank)
        finally:
            dist.destroy_process_group()
            self._queue.put(None)

    def __iter__(self):
        processes, buffer = [], []
        terminate_counter = 0
        try:
            for rank in range(self.world_size):
                p = self._context.Process(
                    target=self._worker,
                    args=(self.world_size, rank),
                )
                p.start()
                processes.append(p)

            while terminate_counter < self.world_size:
                output = self._queue.get()
                if output is None:
                    terminate_counter += 1
                    continue
                buffer.append(output)
                if len(buffer) == self.world_size:
                    buffer = default_collator_fn(buffer)
                    yield buffer
                    buffer = []
        finally:
            for p in processes:
                if p.is_alive():
                    p.terminate()
                    p.join()

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            print("All processes have finished.")

    def __call__(self):
        processes = []
        terminate_counter = 0
        try:
            for rank in range(self.world_size):
                p = self._context.Process(
                    target=self._worker,
                    args=(self.world_size, rank),
                )
                p.start()
                processes.append(p)

            while terminate_counter < self.world_size:
                output = self._queue.get()
                if output is None:
                    terminate_counter += 1
                    continue
        finally:
            for p in processes:
                if p.is_alive():
                    p.terminate()
                    p.join()

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            print("All processes have finished.")


def socket_in_use(port: int):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("localhost", port))
        except OSError:
            return True
        finally:
            return False


def default_collator_fn(x: list):
    return tuple(sum(e) / len(e) for e in zip(*x))


class PrefetchLoader:
    def __init__(self, loader: DataLoader):
        self.loader = loader
        self.stream = torch.cuda.Stream()

    def __iter__(self):
        first = True
        next_data = None

        for batch in self.loader:
            with torch.cuda.stream(self.stream):
                next_data = self._to_cuda(batch)
            
            if not first:
                yield data
            else:
                first = False

            torch.cuda.current_stream().wait_stream(self.stream)
            data = next_data

        yield data

    def _to_cuda(self, batch):
        def _move(item):
            return item.cuda(non_blocking=True) if isinstance(item, torch.Tensor) else item
        
        return tuple(_move(item) for item in batch)

    def __len__(self):
        return len(self.loader)
