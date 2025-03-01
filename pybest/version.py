from __future__ import absolute_import, division, print_function
import os.path as op

# Format expected by setup.py and doc/source/conf.py: string of form "X.Y.Z"
_version_major = 0
_version_minor = 1
_version_micro = ''  # use '' for first of series, number for 1 and above
# _version_extra = 'dev'
_version_extra = ''  # Uncomment this for full releases

# Construct full version string from these.
_ver = [_version_major, _version_minor]
if _version_micro:
    _ver.append(_version_micro)
if _version_extra:
    _ver.append(_version_extra)

__version__ = '.'.join(map(str, _ver))

CLASSIFIERS = ["Development Status :: 3 - Alpha",
               "Environment :: Console",
               "Intended Audience :: Science/Research",
               "License :: OSI Approved :: MIT License",
               "Operating System :: OS Independent",
               "Programming Language :: Python",
               "Topic :: Scientific/Engineering"]

# Description should be a one-liner:
description = "pybest: a PYthon package for Beta ESTimation (of single-trial fMRI data)"
NAME = "pybest"
MAINTAINER = "Lukas Snoek"
MAINTAINER_EMAIL = "lukassnoek@gmail.com"
DESCRIPTION = description
URL = "https://github.com/lukassnoek/pybest"
DOWNLOAD_URL = ""
LICENSE = "3-clause BSD"
AUTHOR = "Lukas Snoek"
AUTHOR_EMAIL = "lukassnoek@gmail.com"
PLATFORMS = "OS Independent"
MAJOR = _version_major
MINOR = _version_minor
MICRO = _version_micro
VERSION = __version__
REQUIRES = ["numpy", "scipy", "matplotlib", "pandas","joblib","toml", "nilearn"]
PACKAGE_DATA = {}#'pybest': [op.join('data', '*.tsv')]}
