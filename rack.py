#!/usr/bin/env python3
"""
rack - simple local log-store utility (git-like per-project .rack)

Commands:
  rack init
  rack store 'message' [key=value ...] [-p path] [-rm]    (flags may appear anywhere)
  rack list [--sort field] [--desc]
  rack search msg=... key=value ...
  rack add <hash> key=value ...
  rack burn [-h <hash1> [hash2...]]   # no flags deletes entire .rack
  rack info <hash>
  rack dump <hash> [-o <output_path>] [-rm]
  rack config
"""
import os
import sys
import hashlib
import json
import tarfile
import fnmatch
import shutil
import tempfile, select
from datetime import datetime

# optional import check
try:
    import zstandard as zstd
except Exception:
    sys.exit("Error: 'zstandard' module not found. Install with: pip install zstandard")

DEFAULT_CONFIG = {
    "input_dir": "logs-debug",
    "exclude": ["*.tmp", "./debug/*"],
    "preserve_structure": True,
    "compress_mode": "files",   # "files" or "folder"
    "zstd_level": 3
}

# ---------- helpers ----------
def human_size(nbytes):
    if nbytes is None:
        return "0 B"
    n = float(nbytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0:
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} PB"

def find_rack_root(start_path=None):
    """Walk upward to find a .rack folder with rack.json (like git)."""
    path = os.path.abspath(start_path or os.getcwd())
    while True:
        rack_path = os.path.join(path, ".rack", "rack.json")
        if os.path.isfile(rack_path):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent

def require_rack_root():
    root = find_rack_root()
    if not root:
        sys.exit("❌ Error: no rack project found (run `rack init` first).")
    return root

def get_paths(root):
    rack_dir = os.path.join(root, ".rack")
    config_file = os.path.join(rack_dir, "rack.json")
    store_dir = os.path.join(rack_dir, "store")
    index_file = os.path.join(store_dir, "index.json")
    return rack_dir, config_file, store_dir, index_file

def init_project():
    root = os.getcwd()
    rack_dir, config_file, store_dir, index_file = get_paths(root)

    if os.path.exists(rack_dir):
        sys.exit("❌ Error: .rack already exists here.")
    os.makedirs(rack_dir, exist_ok=True)
    os.makedirs(store_dir, exist_ok=True)

    with open(config_file, "w") as f:
        json.dump(DEFAULT_CONFIG, f, indent=2)
    with open(index_file, "w") as f:
        json.dump({}, f, indent=2)

    print("📦 Initialized empty rack project in .rack/")

def load_config(root):
    _, config_file, _, _ = get_paths(root)
    with open(config_file) as f:
        return json.load(f)

def load_index(root):
    _, _, _, index_file = get_paths(root)
    with open(index_file) as f:
        return json.load(f)

def save_index(root, index):
    _, _, _, index_file = get_paths(root)
    with open(index_file, "w") as f:
        json.dump(index, f, indent=2)

def hash_commit(msg, tags):
    key = msg + "|" + "|".join(f"{k}={v}" for k, v in sorted(tags.items()))
    return hashlib.sha256(key.encode()).hexdigest()[:12]

def compress_file(src, dst, level):
    cctx = zstd.ZstdCompressor(level=level)
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        with cctx.stream_writer(fdst) as compressor:
            shutil.copyfileobj(fsrc, compressor, length=1024*1024)  # 1MB chunks

def decompress_file(src, dst):
    dctx = zstd.ZstdDecompressor()
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        with dctx.stream_reader(fsrc) as reader:
            shutil.copyfileobj(reader, fdst, length=1024*1024)

def status_bar(cur, total, prefix=""):
    width = 30
    if total == 0:
        bar = "-" * width
        print(f"\r{prefix} |{bar}| 0/0", end="", flush=True)
        print()
        return
    filled = int(width * cur / total)
    bar = "█" * filled + "-" * (width - filled)
    print(f"\r{prefix} |{bar}| {cur}/{total}", end="", flush=True)
    if cur == total:
        print()

# ---------- core commands ----------
def check_abort():
    """Return True if user pressed 'q' (kill switch)."""
    if sys.stdin in select.select([sys.stdin], [], [], 0)[0]:
        ch=sys.stdin.read(1)
        if ch.lower()=="q": return True
    return False

# ---------- core ----------
def store(msg,tags,override_path=None,remove=False):
    root=require_rack_root()
    _,_,store_dir,_=get_paths(root)
    cfg=load_config(root)
    logs_dir=os.path.abspath(override_path) if override_path else os.path.join(root,cfg["input_dir"])
    if not os.path.isdir(logs_dir): sys.exit(f"❌ Error: input dir not found: {logs_dir}")

    index=load_index(root)
    h=hash_commit(msg,tags)
    if h in index: sys.exit(f"❌ Error: duplicate commit detected {h}")

    tmp_commit_dir=os.path.join(store_dir,"_"+h)
    final_commit_dir=os.path.join(store_dir,h)
    if os.path.exists(tmp_commit_dir) or os.path.exists(final_commit_dir):
        sys.exit(f"❌ Error: commit directory for {h} already exists")

    os.makedirs(tmp_commit_dir,exist_ok=True)

    files=[]; 
    for wr,_,fns in os.walk(logs_dir):
        for fn in fns:
            rel=os.path.relpath(os.path.join(wr,fn),logs_dir)
            if any(fnmatch.fnmatch(rel,pat) for pat in cfg.get("exclude",[])): continue
            files.append((wr,fn))

    total_size=0; count=len(files)
    try:
        if cfg.get("compress_mode","files")=="folder":
            tar_path=os.path.join(tmp_commit_dir,"logs.tar")
            with tarfile.open(tar_path,"w") as tar:
                for i,(wr,fn) in enumerate(files,1):
                    if check_abort(): raise KeyboardInterrupt
                    rel=os.path.relpath(os.path.join(wr,fn),logs_dir)
                    arc=rel if cfg.get("preserve_structure",True) else fn
                    tar.add(os.path.join(wr,fn),arcname=arc)
                    status_bar(i,count,prefix="📦 Packing")
            # compress tar
            lvl=cfg.get("zstd_level",3)
            with open(tar_path,"rb") as fsrc, open(tar_path+".zst","wb") as fdst:
                fdst.write(zstd.ZstdCompressor(level=lvl).compress(fsrc.read()))
            total_size=os.path.getsize(tar_path+".zst"); os.remove(tar_path)
        else: # files
            lvl=cfg.get("zstd_level",3)
            for i,(wr,fn) in enumerate(files,1):
                if check_abort(): raise KeyboardInterrupt
                rel=os.path.relpath(os.path.join(wr,fn),logs_dir)
                dst=os.path.join(tmp_commit_dir,rel+".zst") if cfg.get("preserve_structure",True) else os.path.join(tmp_commit_dir,fn+".zst")
                os.makedirs(os.path.dirname(dst),exist_ok=True)
                compress_file(os.path.join(wr,fn),dst,lvl)
                total_size+=os.path.getsize(dst)
                status_bar(i,count,prefix="📦 Compressing")

        now=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rel_input=os.path.relpath(logs_dir,root)
        index[h]={"msg":msg,"date":now,"size_bytes":total_size,"files":count,
                  "path":os.path.relpath(final_commit_dir,root),"input_dir":rel_input,"tags":tags}
        save_index(root,index)

        shutil.move(tmp_commit_dir,final_commit_dir)
        tags_str=", ".join(f"{k}={v}" for k,v in tags.items())
        print(f"✅ Stored {h} | {now} | {human_size(total_size)} | {count} files | {msg} | {tags_str}")

        if remove:
            for fn in os.listdir(logs_dir):
                fp=os.path.join(logs_dir,fn)
                shutil.rmtree(fp) if os.path.isdir(fp) else os.remove(fp)
            print(f"🗑️ Cleared contents of {logs_dir}")
    except KeyboardInterrupt:
        shutil.rmtree(tmp_commit_dir,ignore_errors=True)
        print("\n❌ Aborted by user (q pressed). Cleaned up temp data.")
    except Exception as e:
        shutil.rmtree(tmp_commit_dir,ignore_errors=True)
        raise e

def dump(commit_hash,outdir=None,remove=False):
    root=require_rack_root(); _,_,store_dir,_=get_paths(root)
    cfg=load_config(root); index=load_index(root)
    if commit_hash not in index: sys.exit(f"❌ Error: commit {commit_hash} not found")
    commit_dir=os.path.join(store_dir,commit_hash)
    if not os.path.isdir(commit_dir): sys.exit(f"❌ Error: commit dir missing {commit_hash}")

    outdir=outdir or os.path.join(root,index[commit_hash].get("input_dir",cfg.get("input_dir")))
    outdir=os.path.abspath(outdir); os.makedirs(outdir,exist_ok=True)

    files_extracted=[]
    try:
        tar_zst=os.path.join(commit_dir,"logs.tar.zst")
        if os.path.isfile(tar_zst):
            with tempfile.NamedTemporaryFile(delete=False) as tmp: tmp_tar=tmp.name
            with open(tar_zst,"rb") as fsrc, open(tmp_tar,"wb") as fdst:
                fdst.write(zstd.ZstdDecompressor().decompress(fsrc.read()))
            with tarfile.open(tmp_tar,"r") as tar:
                members=[m for m in tar.getmembers() if m.isreg() or m.isdir()]
                total=len(members)
                for i,m in enumerate(members,1):
                    if check_abort(): raise KeyboardInterrupt
                    tar.extract(m,path=outdir)
                    if m.isreg(): files_extracted.append(m.name)
                    status_bar(i,total,prefix="📤 Extracting")
            os.remove(tmp_tar)
        else:
            zst_files=[]
            for wr,_,fns in os.walk(commit_dir):
                for fn in fns:
                    if fn.endswith(".zst"): zst_files.append(os.path.join(wr,fn))
            total=len(zst_files)
            for i,src in enumerate(zst_files,1):
                if check_abort(): raise KeyboardInterrupt
                rel=os.path.relpath(src,commit_dir)[:-4]
                dst=os.path.join(outdir,rel); os.makedirs(os.path.dirname(dst),exist_ok=True)
                decompress_file(src,dst); files_extracted.append(rel)
                status_bar(i,total,prefix="📤 Extracting")

        print(f"✅ Dumped {len(files_extracted)} files into {outdir}")
        if remove:
            shutil.rmtree(commit_dir); index.pop(commit_hash,None); save_index(root,index)
            print(f"🗑️ Removed stored commit {commit_hash}")
    except KeyboardInterrupt:
        print("\n❌ Aborted by user (q pressed). Partial dump may exist in", outdir)

def list_commits(sort_field=None, desc=False):
    root = require_rack_root()
    index = load_index(root)
    if not index:
        print("📥 Rack is empty!")
        return
    items = list(index.items())

    # sorting rules: date, size_bytes, files, or tag:<name>
    if sort_field:
        if sort_field in ("date", "size_bytes", "files"):
            items.sort(key=lambda kv: kv[1].get(sort_field, ""), reverse=desc)
        elif sort_field.startswith("tag:"):
            tag_name = sort_field.split(":", 1)[1]
            items.sort(key=lambda kv: kv[1].get("tags", {}).get(tag_name, ""), reverse=desc)
        else:
            items.sort(key=lambda kv: kv[1].get(sort_field, ""), reverse=desc)

    for h, data in items:
        tags = ", ".join([f"{k}={v}" for k, v in data.get("tags", {}).items()])
        print(f"{h} | {data['date']} | {human_size(data['size_bytes'])} | "
              f"{data.get('files', 0)} files | {data['msg']} | {tags}")

def info(commit_hash):
    root = require_rack_root()
    index = load_index(root)
    if commit_hash not in index:
        sys.exit(f"❌ Error: store {commit_hash} not found.")
    data = index[commit_hash]
    print(f"ℹ️  Store: {commit_hash}")
    print(f"   📅 Date: {data.get('date')}")
    print(f"   📝 Message: {data.get('msg')}")
    print(f"   📂 Path: {data.get('path')}")
    print(f"   ↪️  Input dir: {data.get('input_dir')}")
    print(f"   📊 Size: {human_size(data.get('size_bytes'))}")
    print(f"   📑 Files: {data.get('files', 0)}")
    if data.get("tags"):
        print(f"   🔖 Tags: {', '.join([f'{k}={v}' for k, v in data['tags'].items()])}")

def search(filters):
    root = require_rack_root()
    index = load_index(root)
    results = []
    for h, data in index.items():
        ok = True
        for k, v in filters.items():
            if k == "msg":
                if v.lower() not in (data.get("msg") or "").lower():
                    ok = False
                    break
            else:
                if str(data.get("tags", {}).get(k, "")).lower() != str(v).lower():
                    ok = False
                    break
        if ok:
            results.append((h, data))
    if not results:
        sys.exit("❌ Error: no matching stores found.")
    for h, data in results:
        tags = ", ".join([f"{k}={v}" for k, v in data.get("tags", {}).items()])
        print(f"{h} | {data['date']} | {human_size(data['size_bytes'])} | "
              f"{data.get('files',0)} files | {data['msg']} | {tags}")

def add_tags(commit_hash, new_tags):
    root = require_rack_root()
    _, _, store_dir, _ = get_paths(root)
    index = load_index(root)
    if commit_hash not in index:
        sys.exit(f"❌ Error: store {commit_hash} not found.")
    old_entry = index[commit_hash]
    merged = {**old_entry.get("tags", {}), **new_tags}
    new_hash = hash_commit(old_entry["msg"], merged)

    # collision
    if new_hash != commit_hash and new_hash in index:
        sys.exit(f"❌ Error: Adding these tags would create duplicate store ({new_hash}). Aborting.")

    # if hash unchanged: just update tags
    if new_hash == commit_hash:
        index[commit_hash]["tags"].update(new_tags)
        save_index(root, index)
        print(f"🔖 Tags updated for {commit_hash}: {new_tags}")
        return

    # otherwise rename directory and move index entry
    old_dir = os.path.join(store_dir, commit_hash)
    new_dir = os.path.join(store_dir, new_hash)
    if os.path.exists(new_dir):
        sys.exit(f"❌ Error: target store directory {new_hash} already exists. Aborting.")
    try:
        shutil.move(old_dir, new_dir)
    except Exception as e:
        sys.exit(f"❌ Error moving store dir: {e}")

    # create new index entry (keep original date), update tags and path
    new_entry = dict(old_entry)
    new_entry["tags"] = merged
    new_entry["path"] = os.path.relpath(new_dir, root)
    index[new_hash] = new_entry
    # remove old
    del index[commit_hash]
    save_index(root, index)
    print(f"🔖 Tags added and store renamed: {commit_hash} -> {new_hash}")

def burn(hashes=None):
    root = require_rack_root()
    rack_dir, _, store_dir, _ = get_paths(root)

    if not hashes:  # delete entire .rack directory
        confirm = input("⚠️ Really delete entire .rack folder and all contents? (y/N): ")
        if confirm.lower() == "y":
            shutil.rmtree(rack_dir)
            print("🔥 Entire .rack folder deleted.")
        else:
            print("❌ Aborted.")
        return

    index = load_index(root)
    for h in hashes:
        commit_dir = os.path.join(store_dir, h)
        if os.path.isdir(commit_dir):
            shutil.rmtree(commit_dir)
            index.pop(h, None)
            print(f"🔥 Deleted {h}")
        else:
            print(f"❌ Not found: {h}")
    save_index(root, index)

def config_show():
    root = require_rack_root()
    cfg = load_config(root)
    print("⚙️ Current rack config:")
    print(json.dumps(cfg, indent=2))

# ---------- argument parsing helpers ----------
def parse_kv(args):
    tags = {}
    for t in args:
        if "=" not in t:
            sys.exit(f"❌ Error: invalid syntax '{t}' (expected key=value)")
        k, v = t.split("=", 1)
        tags[k.strip()] = v.strip()
    return tags

def extract_flags_and_positionals(argv):
    """
    Given argv slice for a command (e.g. sys.argv[2:]), return a dict with:
      - flags: simple flags present (set)
      - opts: mapping of option->value for -p/-o/-m etc
      - kvs: list of key=value strings
      - positionals: list of standalone tokens (not flags and not key=values)
    This parser is intentionally permissive so flags/options can appear anywhere.
    """
    flags = set()
    opts = {}
    kvs = []
    positionals = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-rm", "--rm"):
            flags.add("rm"); i += 1; continue
        if a in ("-p", "--path"):
            if i+1 >= len(argv): sys.exit("❌ Error: -p requires a path")
            opts["p"] = argv[i+1]; i += 2; continue
        if a in ("-o", "--out"):
            if i+1 >= len(argv): sys.exit("❌ Error: -o requires a path")
            opts["o"] = argv[i+1]; i += 2; continue
        if a in ("-m", "--message"):
            if i+1 >= len(argv): sys.exit("❌ Error: -m requires a message")
            opts["m"] = argv[i+1]; i += 2; continue
        if a in ("-h", "--hash") and False:
            # avoid colliding with burn -h flag usage. we don't use -h for hash to keep parity
            pass
        # key=value
        if "=" in a and not a.startswith("-"):
            kvs.append(a); i += 1; continue
        # standalone token (could be message or hash)
        if not a.startswith("-"):
            positionals.append(a); i += 1; continue
        # unknown flag
        sys.exit(f"❌ Error: unknown argument '{a}'")
    return {"flags": flags, "opts": opts, "kvs": kvs, "pos": positionals}

def print_usage_and_exit():
    print("Usage:")
    print("  rack init")
    print("  rack store 'message' [key=value ...] [-p path] [-rm]")
    print("  rack list [--sort field] [--desc]")
    print("  rack search msg=... key=value ...")
    print("  rack add <hash> key=value ...")
    print("  rack burn [-h <hash1> [hash2...]]  # no flags deletes entire .rack")
    print("  rack info <hash>")
    print("  rack dump <hash> [-o <output_path>] [-rm]")
    print("  rack config")
    sys.exit(1)

# ---------- main ----------
def main():
    if len(sys.argv) < 2:
        print_usage_and_exit()

    cmd = sys.argv[1]

    if cmd == "init":
        init_project()
        return

    if cmd == "store":
        # parse flexible args (everything after "store")
        parts = extract_flags_and_positionals(sys.argv[2:])
        # message: priority: -m opt, then first positional
        if "m" in parts["opts"]:
            message = parts["opts"]["m"]
        elif parts["pos"]:
            message = parts["pos"][0]
        else:
            sys.exit("❌ Error: store requires a message (use -m or provide as positional).")
        override_path = parts["opts"].get("p")
        remove = ("rm" in parts["flags"])
        tags = parse_kv(parts["kvs"]) if parts["kvs"] else {}
        store(message, tags, override_path, remove)
        return

    if cmd == "list":
        sort_field, desc = None, False
        if "--sort" in sys.argv:
            idx = sys.argv.index("--sort")
            if idx + 1 >= len(sys.argv):
                sys.exit("❌ Error: --sort requires a field")
            sort_field = sys.argv[idx + 1]
        if "--desc" in sys.argv:
            desc = True
        list_commits(sort_field, desc)
        return

    if cmd == "search":
        if len(sys.argv) < 3:
            sys.exit("❌ Error: search requires at least one filter")
        filters = parse_kv(sys.argv[2:])
        search(filters)
        return

    if cmd == "add":
        # flexible: commit hash may be positional anywhere; require at least one kv after it
        parts = extract_flags_and_positionals(sys.argv[2:])
        pos = parts["pos"]
        if not pos:
            sys.exit("❌ Error: add requires a hash and at least one key=value")
        commit_hash = pos[0]
        if not parts["kvs"]:
            sys.exit("❌ Error: add requires at least one key=value")
        new_tags = parse_kv(parts["kvs"])
        add_tags(commit_hash, new_tags)
        return

    if cmd == "burn":
        if len(sys.argv) > 2:
            hashes = sys.argv[2:]
            burn(hashes)
        else:
            burn()
        return

    if cmd == "info":
        # flexible: accept hash as positional or as first non-kv token
        parts = extract_flags_and_positionals(sys.argv[2:])
        if parts["pos"]:
            h = parts["pos"][0]
        else:
            sys.exit("❌ Error: info requires a hash")
        info(h)
        return

    if cmd == "dump":
        # flexible parsing: hash may be positional, flags -o and -rm anywhere
        parts = extract_flags_and_positionals(sys.argv[2:])
        if parts["pos"]:
            commit_hash = parts["pos"][0]
        else:
            sys.exit("❌ Error: dump requires a commit hash")
        outdir = parts["opts"].get("o")
        remove = ("rm" in parts["flags"])
        dump(commit_hash, outdir, remove)
        return

    if cmd == "config":
        config_show()
        return

    # unknown command
    sys.exit(f"❌ Error: unknown command '{cmd}'\nStart with `rack init`")

if __name__ == "__main__":
    main()
