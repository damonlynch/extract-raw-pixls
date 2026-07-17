#  SPDX-FileCopyrightText: 2026 Damon Lynch <damonlynch@gmail.com>
#  SPDX-License-Identifier: GPL-3.0-or-later

"""
Load program configuration from a .env file,  optionally overridden by environment
variables.

The .env file's format is specified here:
https://github.com/theskumar/python-dotenv

The .env file (overridden by optional environment variables of the same name) should
contain three keys:

1. RAW_FILE_REPOSITORY: An absolute path which holds the Git repository and the RAW
   files
2. GIT_DIRECTORY: A relative path within RAW_FILE_REPOSITORY, or (optionally) an
   absolute path anywhere on the file system
3. RAW_FILES: A relative path within RAW_FILE_REPOSITORY, or (optionally) an absolute
   path anywhere on the file system

All three keys must contain non-empty values, e.g.

RAW_FILE_REPOSITORY="D:/raw.pixls.us/"
GIT_DIRECTORY="git_repo"
RAW_FILES="raw_files"
"""


import os
from pathlib import Path
from typing import NamedTuple, cast

from dotenv import dotenv_values


class Config(NamedTuple):
    RAW_FILE_REPOSITORY: Path
    GIT_DIRECTORY: Path
    RAW_FILES: Path


def repository_directories() -> Config:
    """
    Load program keys and values from .env file, optionally overwriting them with
    environment variables.
    :return: Config NamedTuple containing key value pairs
    """

    config = {
        **dotenv_values(".env"),  # load shared development variables
        **os.environ,  # override loaded values with environment variables
    }
    for key in ("RAW_FILE_REPOSITORY", "RAW_FILES", "GIT_DIRECTORY"):
        if key not in config:
            raise KeyError(f"{key} not found in .env file.")
        if not config[key]:
            raise ValueError(f"{key} in .env file has no value.")

    key = "RAW_FILE_REPOSITORY"
    repository = Path(cast(str, config[key]))
    if not repository.is_absolute():
        raise ValueError(f"{key} value {config[key]} is not an absolute path.")

    git_directory = Path(cast(str, config["GIT_DIRECTORY"]))
    if not git_directory.is_absolute():
        git_directory = repository / git_directory

    raw_files = Path(cast(str, config["RAW_FILES"]))
    if not raw_files.is_absolute():
        raw_files = repository / raw_files

    return Config(
        RAW_FILE_REPOSITORY=repository,
        GIT_DIRECTORY=git_directory,
        RAW_FILES=raw_files,
    )
