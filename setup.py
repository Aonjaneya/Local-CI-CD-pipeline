from setuptools import setup, find_packages

setup(
    name="cicd-sample-project",
    version="0.1.0",
    description="A minimal sample project used to test a custom CI/CD pipeline.",
    packages=find_packages(include=["src", "src.*"]),
    python_requires=">=3.7",
)
