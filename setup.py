#!/usr/bin/env python

from setuptools import setup, find_packages

REQUIREMENTS = [
    line.strip() for line in open("requirements.txt").readlines() if line[0].isalpha()
]

setup(
    name="esp-config-updater",
    version="0.1",
    author="Adam Groszer",
    url="https://github.com/",
    description="DESCRIPTION",
    license="GPL 3.0",
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Environment :: Console",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
    ],
    packages=find_packages("src"),
    package_dir={"": "src"},
    package_data={},
    install_requires=REQUIREMENTS,
    entry_points={
        "console_scripts": [
            "config = espcfg.main:config",
            "discover = espcfg.main:discover",
        ]
    },
)
