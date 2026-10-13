import pcbnew
import json
import sys


def extract_kicad_netlist(board_path, output_json_path):
    print(f"Loading board: {board_path}")
    try:
        board = pcbnew.LoadBoard(board_path)
    except Exception as e:
        print(f"Failed to load board: {e}")
        sys.exit(1)

    net_topology = {}

    # Iterate through every physical footprint on the PCB
    for footprint in board.GetFootprints():
        ref = footprint.GetReference()  # e.g., 'U1', 'R15'
        val = footprint.GetValue()  # e.g., 'STM32F407VETx', '330R'

        # Iterate through every pad (pin) on the footprint
        for pad in footprint.Pads():
            pad_num = pad.GetPadName()
            net_name = pad.GetNetname()

            # Ignore unconnected pads
            if not net_name or net_name == "":
                continue

            # Initialize the net list if it doesn't exist
            if net_name not in net_topology:
                net_topology[net_name] = {
                    "nodes": []
                }

            # Append this specific pin to the net
            net_topology[net_name]["nodes"].append({
                "component": ref,
                "value": val,
                "pin": pad_num
            })

    # Export the mapped topology to JSON
    with open(output_json_path, 'w') as f:
        json.dump(net_topology, f, indent=4)

    print(f"Successfully exported {len(net_topology)} nets to {output_json_path}")


if __name__ == "__main__":
    # Replace with your actual KiCad PCB file path
    pcb_file = "automation.kicad_pcb"
    json_file = "digital_twin_netlist.json"

    extract_kicad_netlist(pcb_file, json_file)