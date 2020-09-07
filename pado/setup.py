import os

from setuptools import setup

setup(
    use_scm_version={
        "write_to": "pado/_version.py",
        "version_scheme": "post-release",
        "relative_to": os.path.dirname(__file__),
        "root": "..",
    }
)