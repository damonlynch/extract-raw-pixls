# extract-raw-pixls

## Purpose

The raison d'être of `extract-raw-pixls` is to download the entire contents of the Git repository of RAW files found at [Pixls.us](https://raw.pixls.us/) to a Windows file system. As such, it renames files and paths that contain characters that are illegal on Windows file systems.

To achieve this goal while maintaining all the advantages of a Git repository, this program uses Git to download the entire raw.pixls.us Git repository without checking out the working tree. It then manually extracts the files from the repository's `.git` folder, naming them and their paths appropriately. 

This requires storage space that is double the size of the repository's actual RAW files, because the RAW files are stored twice: once in the repository's `.git` folder, and again as a normal file on your file system. At the time of writing, about 112 GB is required in total.

## Installation

If you are running Windows, open a PowerShell terminal window and follow these steps:

1. Install [Git for Windows](https://git-scm.com/install/windows)
2. Install [Git Large File Storage (LFS)](https://git-lfs.com/) &mdash; do not forget the `git lfs install` step.
3. Install [uv](https://docs.astral.sh/uv/getting-started/installation/#__tabbed_1_2)
4. Use `uv` to install [Hatch](https://hatch.pypa.io/latest/):
```console
uv tool install hatch
```
5. Clone this repository into a directory where you plan to run the program:
```console
git clone https://github.com/damonlynch/extract-raw-pixls.git
```
6. Create a file named `.env` inside the `extract-raw-pixls` directory. This file should specify the location of the RAW file repository and the names of its two subdirectories:
```ini
RAW_FILE_REPOSITORY="D:/directory/location/of/repository/"
GIT_DIRECTORY="git_repo"
RAW_FILES="raw_files"
```
You must use the key names exactly as above. However, you can specify whatever key values you want, with the only limitation being that `RAW_FILE_REPOSITORY` is an absolute path. It is suggested but not mandated that `GIT_DIRECTORY` and `RAW_FILES` are both relative paths, meaning they will be created as subdirectories of  `RAW_FILE_REPOSITORY`.

The program will create these directories if they do not exist.

## Usage

To download the raw.pixls.us Git repository into a `.git` folder within the `GIT_DIRECTORY` specified above, and then extract all the RAW files into subdirectories within `RAW_FILES`, run:
```console
hatch run extract-raw-pixls
```

Whenever the raw.pixls.us Git repository is updated, you can rerun `extract-raw-pixls` and it will apply the changes without needing to redownload the entire repository.

Add the `--dry-run` command line argument to preview the RAW files it will add, update, or remove: 
```console
hatch run extract-raw-pixls --dry-run
```

Please note: irrespective of whether you run the program with or without the `--dry-run` argument, it ensures that the entire contents of the raw.pixls.us Git repository (including all RAW files) are saved locally.

## License

[GPL3 or later](https://choosealicense.com/licenses/gpl-3.0/).
