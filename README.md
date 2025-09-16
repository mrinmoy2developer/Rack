# Rack

`rack` is a simple, git-like local log storage utility for managing and compressing logs within any project.  
It creates a `.rack/` directory (similar to `.git/`) where your log snapshots are stored with metadata, messages, tags, and compression.

---

## Features

- **Initialize** a `.rack` project directory  
- **Store** logs with a message, tags, and optional path override  
- **List** all stored commits with size, file count, and tags  
- **Search** commits by message or tags  
- **Add** new tags to an existing commit  
- **Dump** (restore) stored logs to an output directory  
- **Info** detailed view of a commit (date, size, file count, tags)  
- **Burn** delete specific commits or the entire `.rack`  
- **Config** view project-level configuration  

Compression is handled using [zstd](https://facebook.github.io/zstd/) with support for both **per-file** and **tarball** modes.

---

## Installation

```bash
git clone https://github.com/yourusername/rack.git
cd rack
pip install -r requirements.txt
chmod +x rack.py
# ln -s $(pwd)/rack.py /usr/local/bin/rack
```
##Usage 

## Commands
  -- add <input_dir> -m “”
Store a snapshot of a folder with a commit message.
Flags:
	•	-o <output_dir> → specify custom repo location (default: <input_dir>/.rack/)
	•	-rm → remove contents of <input_dir> after storing
	•	-p → show progress bar during storage
	•	info <hash>
Show metadata about a specific commit (author, time, size, files).
	•	**dump <hash> -o <output_dir>**   Extract a commit into a directory.   If -ois not provided, dumps into<input_dir>/`.
Shows a progress bar during extraction.
	•	list
Display all stored commits with details.
	•	burn […]
Delete commits by hash.
Without arguments, deletes the entire repository (with confirmation).
