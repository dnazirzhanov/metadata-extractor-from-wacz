"""Independent before/after fingerprint of the archives an extraction will read.

The extractor has its own per-archive stat fence (output.ArchiveFingerprint).
This is the OUTSIDE check: it does not import that code and it covers the whole
sampled set at once, so a mutation the fence somehow missed still shows up.
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path.home() / "causalia-article-extractor" / "src"))
from causalia_extractor.identity import iter_wacz_files

root = Path("/mnt/hdd/c0cshf/causalia/pages")
per_outlet = int(sys.argv[1])
out = Path(sys.argv[2])

rows = {}
for outlet in sorted(d.name for d in root.iterdir() if d.is_dir() and "." in d.name):
    n = 0
    for wacz in iter_wacz_files(root, outlet=outlet):
        st = wacz.stat()
        rows[str(wacz)] = [st.st_size, st.st_mtime_ns, st.st_ino]
        n += 1
        if n >= per_outlet:
            break
out.write_text(json.dumps(rows, indent=0, sort_keys=True))
print(f"{len(rows)} archives fingerprinted -> {out}")
print(f"total bytes: {sum(v[0] for v in rows.values()):,}")
