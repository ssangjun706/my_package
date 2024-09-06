from setuptools import setup, find_packages

with open("README.md") as fp:
    long_desc = fp.read()

setup(
    name="my_package",
    version="0.0.2",
    packages=find_packages(include=['parallel', 'parallel.*']),
    install_requires=[
        "torch",
    ],
    python_requires=">=3.11",
    long_description=long_desc,
    long_description_content_type="text/markdown",
    url="https://github.com/ssangjun706/my_package",
)
