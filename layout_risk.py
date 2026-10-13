# layout_risk.py
"""
Find UDTs whose memory layout something else depends on.

Reordering members changes byte offsets. Copying by member NAME is safe, but
these copy by BYTE OFFSET and break silently if the layout changes:

  * COP / CPS source or destination      (raw byte copy)
  * MSG local element                    (raw buffer to/from another device)
  * Produced / consumed tags             (the partner holds its own definition)

If UDT A is flagged, every UDT nested inside A is flagged too, because
changing the inner layout changes A's layout.

This is a static text scan of the L5X. It over-approximates rather than miss
a case: a flagged UDT is "check before importing", not "definitely broken".
It cannot see HMI/SCADA or other controllers' projects.
"""
import re
import xml.etree.ElementTree as ET

_COPY_RE = re.compile(r'\b(COP|CPS)\s*\(\s*([^,()\s]+)\s*,\s*([^,()\s]+)\s*,', re.I)
_IDENT   = re.compile(r'[A-Za-z_]\w*')


def _scope_strip(operand: str) -> str:
    # \Program.Tag  ->  Tag
    return re.sub(r'^\\[A-Za-z_]\w*\.', '', operand.strip())


def _resolve_type(operand: str, tag_types: dict, all_udts: dict):
    """Follow Tag.Member[.Member...] through UDT definitions to the operand's type."""
    path = _scope_strip(operand)
    segs = [s for s in re.split(r'\.', re.sub(r'\[[^\]]*\]', '', path)) if s]
    if not segs:
        return None
    types = tag_types.get(segs[0])
    if not types:
        return None
    cur = next(iter(types)) if len(types) == 1 else None
    resolved = set(types) if cur is None else {cur}
    if cur is None:
        return resolved            # ambiguous name across scopes: flag all
    for seg in segs[1:]:
        if seg.isdigit():          # bit access
            break
        udt = all_udts.get(cur)
        if not udt:
            break
        nxt = next((m["type"] for m in udt["members"] if m["name"] == seg), None)
        if nxt is None:
            break
        cur = nxt
    return {cur}


def scan_layout_risk(l5x_text: str, all_udts: dict) -> dict:
    """
    Return {udt_name: [reason, ...]} for UDTs whose layout is depended on.
    Empty dict for single-UDT exports (they contain no logic or tags).
    """
    try:
        root = ET.fromstring(l5x_text)
    except ET.ParseError:
        return {}

    # Tag / parameter / local-tag name -> set of DataTypes (names can repeat across scopes)
    tag_types: dict = {}
    for el in root.iter():
        if el.tag in ("Tag", "Parameter", "LocalTag"):
            n, t = el.get("Name"), el.get("DataType")
            if n and t:
                tag_types.setdefault(n, set()).add(t)

    risk: dict = {}

    def flag(types, reason):
        for t in types or ():
            if t in all_udts:
                lst = risk.setdefault(t, [])
                if reason not in lst:
                    lst.append(reason)

    # Produced / consumed tags
    for el in root.iter("Tag"):
        tt = el.get("TagType")
        if tt in ("Produced", "Consumed"):
            flag({el.get("DataType")}, f"{tt.lower()} tag {el.get('Name')}")

    # COP / CPS in any routine text (RLL rungs, ST lines; CDATA is plain text to ET)
    for el in root.iter():
        if el.tag in ("Text", "Line") and el.text:
            for instr, src, dst in _COPY_RE.findall(el.text):
                for op in (src, dst):
                    flag(_resolve_type(op, tag_types, all_udts),
                         f"{instr.upper()} operand {_scope_strip(op)}")

    # MSG local element
    for el in root.iter("MessageParameters"):
        le = el.get("LocalElement")
        if le:
            flag(_resolve_type(le, tag_types, all_udts), f"MSG local element {le}")

    # Propagate to nested UDTs
    changed = True
    while changed:
        changed = False
        for parent in list(risk):
            for m in all_udts.get(parent, {}).get("members", []):
                child = m["type"]
                if child in all_udts:
                    reason = f"nested inside {parent}"
                    lst = risk.setdefault(child, [])
                    if reason not in lst:
                        lst.append(reason)
                        changed = True
    return risk
