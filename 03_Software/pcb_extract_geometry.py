"""
Runs in KiCad Python. Extracts board geometry to JSON.
No matplotlib, no zmq — just pcbnew + json.
"""
import pcbnew
import json

BOARD_FILE = "automation.kicad_pcb"
OUTPUT_JSON = "pcb_geometry.json"


def nm_to_mm(nm):
    return float(nm) / 1e6


def extract():
    print(f"[*] Loading {BOARD_FILE}")
    board = pcbnew.LoadBoard(BOARD_FILE)

    # --- Board outline ---
    bbox = board.GetBoardEdgesBoundingBox()
    outline = {
        "x_mm": nm_to_mm(bbox.GetX()),
        "y_mm": nm_to_mm(bbox.GetY()),
        "w_mm": nm_to_mm(bbox.GetWidth()),
        "h_mm": nm_to_mm(bbox.GetHeight()),
        "layer_count": board.GetCopperLayerCount(),
    }

    # --- Tracks (copper traces) ---
    tracks = []
    vias = []
    for t in board.GetTracks():
        # Handle vias separately — they have GetWidth(layer) signature
        if isinstance(t, pcbnew.PCB_VIA):
            pos = t.GetPosition()
            via_width = nm_to_mm(t.GetFrontWidth())
            via_drill = nm_to_mm(t.GetDrillValue())
            vias.append({
                "x_mm": nm_to_mm(pos.x),
                "y_mm": nm_to_mm(pos.y),
                "diameter_mm": via_width,
                "drill_mm": via_drill,
            })
            continue

        # Regular tracks
        layer = t.GetLayer()
        if layer == pcbnew.F_Cu:
            layer_name = "F_Cu"
        elif layer == pcbnew.B_Cu:
            layer_name = "B_Cu"
        else:
            continue
        s = t.GetStart()
        e = t.GetEnd()
        tracks.append({
            "layer": layer_name,
            "x1": nm_to_mm(s.x),
            "y1": nm_to_mm(s.y),
            "x2": nm_to_mm(e.x),
            "y2": nm_to_mm(e.y),
            "width_mm": nm_to_mm(t.GetWidth()),
            "net_name": t.GetNetname(),
        })

    # --- Footprints, pads, refdes, graphical layers ---
    footprints = []
    for fp in board.GetFootprints():
        pos = fp.GetPosition()
        entry = {
            "ref": fp.GetReference(),
            "value": fp.GetValue(),
            "x_mm": nm_to_mm(pos.x),
            "y_mm": nm_to_mm(pos.y),
            "angle_deg": fp.GetOrientationDegrees(),
            "layer": "F_Cu" if fp.GetLayer() == pcbnew.F_Cu else "B_Cu",
            "pads": [],
            "fab_shapes": [],
            "silk_shapes": [],
            "courtyard_shapes": [],  # NEW
        }

        # Pads
        for pad in fp.Pads():
            p = pad.GetPosition()
            s = pad.GetSize()
            entry["pads"].append({
                "x_mm": nm_to_mm(p.x),
                "y_mm": nm_to_mm(p.y),
                "w_mm": nm_to_mm(s.x),
                "h_mm": nm_to_mm(s.y),
                "angle_deg": pad.GetOrientationDegrees(),
                "net_name": pad.GetNetname(),
                "pad_name": pad.GetPadName(),
            })

        # Graphical items on F.Fab and F.SilkS
        for item in fp.GraphicalItems():
            layer = item.GetLayer()
            if layer not in (pcbnew.F_Fab, pcbnew.F_SilkS, pcbnew.F_CrtYd):
                continue

            shape = None

            # --- TEXT ---
            if isinstance(item, pcbnew.PCB_TEXT):
                pos = item.GetPosition()
                shape = {
                    "type": "text",
                    "x": nm_to_mm(pos.x),
                    "y": nm_to_mm(pos.y),
                    "text": item.GetText(),
                    "size": nm_to_mm(item.GetTextSize().x),
                }

            # --- SHAPES (lines, rects, circles, arcs, polys) ---
            elif isinstance(item, pcbnew.PCB_SHAPE):
                shape_type = item.GetShape()

                if shape_type == pcbnew.SHAPE_T_SEGMENT:
                    s = item.GetStart()
                    e = item.GetEnd()
                    shape = {
                        "type": "line",
                        "x1": nm_to_mm(s.x), "y1": nm_to_mm(s.y),
                        "x2": nm_to_mm(e.x), "y2": nm_to_mm(e.y),
                        "width": nm_to_mm(item.GetWidth()),
                    }
                elif shape_type == pcbnew.SHAPE_T_RECT:
                    s = item.GetStart()
                    e = item.GetEnd()
                    shape = {
                        "type": "rect",
                        "x1": nm_to_mm(s.x), "y1": nm_to_mm(s.y),
                        "x2": nm_to_mm(e.x), "y2": nm_to_mm(e.y),
                        "width": nm_to_mm(item.GetWidth()),
                        "filled": bool(item.IsFilled()),
                    }
                elif shape_type == pcbnew.SHAPE_T_CIRCLE:
                    c = item.GetCenter()
                    shape = {
                        "type": "circle",
                        "cx": nm_to_mm(c.x), "cy": nm_to_mm(c.y),
                        "r": nm_to_mm(item.GetRadius()),
                        "width": nm_to_mm(item.GetWidth()),
                        "filled": bool(item.IsFilled()),
                    }
                elif shape_type == pcbnew.SHAPE_T_ARC:
                    s = item.GetStart()
                    e = item.GetEnd()
                    shape = {
                        "type": "line",  # approximate arc as straight line for now
                        "x1": nm_to_mm(s.x), "y1": nm_to_mm(s.y),
                        "x2": nm_to_mm(e.x), "y2": nm_to_mm(e.y),
                        "width": nm_to_mm(item.GetWidth()),
                    }
                elif shape_type == pcbnew.SHAPE_T_POLY:
                    try:
                        poly = item.GetPolyShape()
                        if poly.OutlineCount() == 0:
                            continue
                        pts = []
                        chain = poly.Outline(0)
                        for i in range(chain.PointCount()):
                            pt = chain.CPoint(i)
                            pts.append([nm_to_mm(pt.x), nm_to_mm(pt.y)])
                        if len(pts) >= 3:
                            shape = {
                                "type": "poly",
                                "points": pts,
                                "filled": True,
                            }
                    except Exception:
                        continue
                else:
                    continue

            if shape is None:
                continue

            if layer == pcbnew.F_Fab:
                entry["fab_shapes"].append(shape)
            elif layer == pcbnew.F_SilkS:
                entry["silk_shapes"].append(shape)
            else:  # F_CrtYd
                entry.setdefault("courtyard_shapes", []).append(shape)

        footprints.append(entry)

    data = {
        "outline": outline,
        "tracks": tracks,
        "vias": vias,
        "footprints": footprints,
    }

    def json_fallback(obj):
        # KiCad SWIG objects often have .x/.y or are iterable
        if hasattr(obj, "x") and hasattr(obj, "y"):
            return [float(obj.x), float(obj.y)]
        if hasattr(obj, "__iter__"):
            try:
                return [json_fallback(v) for v in obj]
            except Exception:
                pass
        return str(obj)

    with open(OUTPUT_JSON, "w") as f:
        json.dump(data, f, default=json_fallback)

    print(f"[+] {len(tracks)} tracks, {len(vias)} vias, {len(footprints)} footprints -> {OUTPUT_JSON}")


if __name__ == "__main__":
    extract()