#!/data/data/com.termux/files/usr/bin/bash
# Runs OneBill on the phone with NO build step. Needs only Termux.
# Usage: bash termux_run.sh   (run from the folder that contains python/ and assets/)
set -e
pkg install -y python >/dev/null
D="$HOME/onebill_run"; rm -rf "$D"; mkdir -p "$D"
cp -r python/* "$D/"
python - "$D" <<'PY'
import sys, pathlib
d = pathlib.Path(sys.argv[1]); a = pathlib.Path("assets/mobile")
h = (d/"ui/index.html").read_text(encoding="utf-8")
inj = "<style>"+(a/"skin.css").read_text(encoding="utf-8")+"</style><script>"+(a/"skin.js").read_text(encoding="utf-8")+"</script></body>"
(d/"ui/index.html").write_text(h.replace("</body>", inj, 1), encoding="utf-8")
PY
export ONEBILL_DATA="$HOME/OneBill_Data" ONEBILL_NOWINDOW=1 ONEBILL_PORT=8765
echo "Open Chrome and go to  http://127.0.0.1:8765"
cd "$D" && exec python onebill_android_entry.py
