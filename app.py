# app.py — L5X UDT Optimizer web front end
import sys, os, re, uuid, threading, time, webbrowser, json, io, zipfile, datetime
from flask import Flask, request, jsonify, render_template, Response, stream_with_context
from L5XOpt_UDT import (
    extract_all_udt_definitions,
    optimize_and_regenerate_udt,
    _topological_sort,
    _estimate_udt_size,
    _extract_datatype_block,
    _replace_datatype_blocks,
)
from layout_risk import scan_layout_risk


def resource_path(rel):
    base = getattr(sys, "_MEIPASS", os.path.abspath("."))
    return os.path.join(base, rel)


app = Flask(__name__,
            template_folder=resource_path("templates"),
            static_folder=resource_path("static"),
            static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024   # 200 MB upload cap

# ── Session store ────────────────────────────────────────────────────────────
# Sessions hold the uploaded text and finished outputs. Downloads do NOT
# consume them, so a second click works; idle sessions expire after SESSION_TTL.
SESSION_TTL = 30 * 60
_store: dict = {}
_lock = threading.Lock()


def _purge():
    now = time.time()
    with _lock:
        for k in [k for k, v in _store.items() if now - v["touched"] > SESSION_TTL]:
            _store.pop(k, None)


def _get(sid):
    with _lock:
        s = _store.get(sid)
        if s:
            s["touched"] = time.time()
        return s


# ── Naming ───────────────────────────────────────────────────────────────────
# Studio 5000 limits project/component names to 40 characters, and a long file
# name is rejected on import. Keep "<name>_opt" inside that limit.
NAME_LIMIT = 40


def sanitize_filename(name):
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", name.strip())


def opt_name(base: str) -> str:
    base = sanitize_filename(base) or "Program"
    return base[:NAME_LIMIT - 4] + "_opt"


# ── Helpers ──────────────────────────────────────────────────────────────────
def _visible_members(members):
    return [{"name": m["name"], "type": m["type"], "dim": m.get("dimension", 0)}
            for m in members
            if not m.get("hidden") and not m["name"].startswith("ZZZZZZZZZZ")]


_MEMBER_RE = re.compile(r'<Member\b[^>]*?\bName="([^"]+)"[^>]*?\bDataType="([^"]+)"[^>]*?\bDimension="(\d+)"[^>]*?\bHidden="(true|false)"')


def _members_from_block(block: str):
    out = []
    for name, dtype, dim, hidden in _MEMBER_RE.findall(block or ""):
        if hidden == "true" or name.startswith("ZZZZZZZZZZ"):
            continue
        out.append({"name": name, "type": dtype, "dim": int(dim)})
    return out


def _read_upload():
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".l5x"):
        return None, None, ("Choose a .L5X file exported from Studio 5000.", 400)
    raw = f.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, None, ("The file is not UTF-8. Re-export it from Studio 5000.", 400)
    return os.path.splitext(f.filename)[0], text, None


# ── Routes ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    """Single upload for both file kinds. Returns a preview and a session id."""
    _purge()
    orig, content, err = _read_upload()
    if err:
        return jsonify({"success": False, "error": err[0]}), err[1]
    try:
        parsed = extract_all_udt_definitions(content)
    except Exception as e:
        return jsonify({"success": False, "error": f"Could not read the L5X: {e}"}), 400
    if "error" in parsed:
        return jsonify({"success": False, "error": parsed["error"]}), 400

    all_udts = parsed["udts"]
    if not all_udts:
        return jsonify({"success": False, "error": "No user-defined data types found in this file."}), 400

    aoi = parsed.get("aoi_registry", {})
    reg = {}
    for n in _topological_sort(all_udts):
        reg[n] = _estimate_udt_size(all_udts[n], reg, aoi)

    single = parsed["l5x_type"] == "single_udt"
    order = [parsed["target"]] if single else _topological_sort(all_udts)

    sid = str(uuid.uuid4())
    with _lock:
        _store[sid] = {
            "touched": time.time(), "content": content, "parsed": parsed,
            "registry": reg, "order": order, "single": single,
            "original": orig, "outputs": None,
        }
    return jsonify({
        "success":   True,
        "session":   sid,
        "kind":      "single_udt" if single else "full_program",
        "source":    orig,
        "aoi_count": len(aoi),
        "udts": [{"name": n, "member_count": len(all_udts[n]["members"]),
                  "size_before": reg.get(n, 0)} for n in order],
    })


@app.route("/stream/<sid>")
def stream(sid):
    session = _get(sid)
    if not session:
        return jsonify({"error": "Session expired. Upload the file again."}), 404

    def sse(d):
        return f"data: {json.dumps(d)}\n\n"

    def generate():
        parsed   = session["parsed"]
        all_udts = parsed["udts"]
        aoi      = parsed.get("aoi_registry", {})
        reg      = session["registry"]
        order    = session["order"]
        single   = session["single"]

        risk = {} if single else scan_layout_risk(session["content"], all_udts)
        changed_blocks, changed_files, rows = {}, {}, []
        yield sse({"type": "start", "total": len(order)})

        for idx, name in enumerate(order):
            udt = all_udts[name]
            yield sse({"type": "progress", "name": name, "status": "processing", "index": idx + 1})
            try:
                res = optimize_and_regenerate_udt(
                    udt, all_udts=all_udts, udt_size_registry=reg, aoi_registry=aoi,
                    aoi_context_xml=parsed.get("aoi_context_xml") if single else None,
                    embed_nested_context=True,
                )
            except Exception as e:                       # never kill the stream
                res = {"success": False, "optimization_needed": False, "error": str(e)}

            ok, need = res.get("success", False), res.get("optimization_needed")
            if res.get("skipped_family"):
                status = "skip"
            elif ok:
                status = "done" if need else "ok"
            elif need is None:
                status = "na"
            else:
                status = "error"

            before = _visible_members(udt["members"])
            after  = before
            if status == "done":
                block = _extract_datatype_block(res["udt_text"])
                after = _members_from_block(block)
                changed_blocks[name] = block
                changed_files[name]  = res["udt_text"]

            row = {
                "type": "progress", "name": name, "index": idx + 1, "status": status,
                "member_count": len(udt["members"]),
                "size_before": res.get("size_before", reg.get(name, 0)),
                "size_after":  res.get("size_after",  reg.get(name, 0)),
                "reason": res.get("error", "") if status in ("na", "skip", "error") else "",
                "skipped_types": res.get("skipped_types", []),
                "aoi_members":   res.get("aoi_members", []),
                "risk":   risk.get(name, []),
                "before": before, "after": after,
            }
            rows.append(row)
            yield sse(row)

        # Build outputs once; they stay downloadable until the session expires.
        outputs = {}
        src = session["original"]
        try:
            if single:
                tgt = order[0]
                if tgt in changed_files:
                    outputs["udt"] = (changed_files[tgt], f"{opt_name(tgt)}.L5X")
            else:
                if changed_blocks:
                    text = _replace_datatype_blocks(session["content"], changed_blocks, all_udts)
                    outputs["program"] = (text, f"{opt_name(src)}.L5X")
                    outputs["zip"] = (_build_zip(src, rows, changed_files),
                                      f"{opt_name(src)}_changed.zip")
        except Exception as e:
            yield sse({"type": "error", "message": f"Could not build the output file: {e}"})
            return

        session["outputs"] = outputs
        saved = sum(max(0, r["size_before"] - r["size_after"]) for r in rows if r["status"] == "done")
        yield sse({
            "type": "done",
            "downloads": {k: v[1] for k, v in outputs.items()},
            "changed": len(changed_blocks), "total": len(order), "total_saved": saved,
            "risk_changed": [r["name"] for r in rows if r["status"] == "done" and r["risk"]],
        })

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _build_zip(src, rows, changed_files) -> bytes:
    buf = io.BytesIO()
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    done = [r for r in rows if r["status"] == "done"]
    lines = [
        "L5X UDT Optimizer - changed UDTs",
        f"Source : {src}.L5X",
        f"Date   : {now}",
        f"Saved  : {sum(max(0, r['size_before'] - r['size_after']) for r in done)} bytes (estimate)",
        "",
        f"{'UDT':<40} {'Before':>8} {'After':>8} {'Saved':>8}  Layout risk",
        "-" * 90,
    ]
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in sorted(done, key=lambda r: r["name"]):
            b, a = r["size_before"], r["size_after"]
            lines.append(f"{r['name']:<40} {b:>7}B {a:>7}B {max(0, b - a):>7}B  "
                         + ("; ".join(r["risk"]) if r["risk"] else "-"))
            zf.writestr(f"{opt_name(r['name'])}.L5X", changed_files[r["name"]])
        lines += [
            "",
            "Import: right-click Data Types > User-Defined > Import Data Type.",
            "Each file is a standalone single-UDT export with its nested types.",
            "Reordering changes byte offsets. Before importing, check every UDT",
            "with a layout risk: COP/CPS copies, MSG buffers, produced/consumed",
            "tags, and any HMI or partner controller that reads it by offset.",
        ]
        zf.writestr("CHANGES.txt", "\r\n".join(lines))
    return buf.getvalue()


@app.route("/download/<sid>/<kind>")
def download(sid, kind):
    s = _get(sid)
    if not s or not s.get("outputs") or kind not in s["outputs"]:
        return jsonify({"error": "This download has expired. Upload the file again."}), 404
    data, fname = s["outputs"][kind]
    mime = "application/zip" if kind == "zip" else "application/octet-stream"
    resp = Response(data, mimetype=mime)
    resp.headers.set("Content-Disposition", f'attachment; filename="{fname}"')
    return resp


@app.errorhandler(413)
def too_large(_):
    return jsonify({"success": False, "error": "File is larger than 200 MB."}), 413


if __name__ == "__main__":
    threading.Timer(1, lambda: webbrowser.open_new("http://127.0.0.1:5001")).start()
    app.run(host="127.0.0.1", port=5001, debug=False, threaded=True)
