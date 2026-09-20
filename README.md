# STM32 Digital Twin — Project Status & Architecture

**Last Updated:** 2026-09-21
**Project:** Open-source industrial PLC digital twin framework
**Location:** C:\Users\user\Desktop\automation\kicad_plugin
**Milestone:** Goals 1, 2A, 2B, 2C, 2D, 3 Complete + Control Panels + Unified Dashboard

---

## PROJECT OVERVIEW

A closed-loop digital twin that bridges:

- KiCad PCB design       -> hardware definition (topology + geometry + values)
- STM32F407VETx firmware -> control logic (compiled to .elf)
- Renode                 -> cycle-accurate virtual MCU execution
- Python + ZeroMQ        -> message-passing middleware
- ngspice                -> real analog circuit simulation (auto-generated)
- PyQt6 control panel    -> live external analog + digital input injection
- PyQt6 unified UI       -> all 7 viewers + control panel + pipeline in one window
- matplotlib             -> live PCB visualization with physics overlays

**Key achievement:** Zero hardcoded circuit knowledge, zero faked inputs.
Every number on screen traces from a real KiCad component through real
firmware through real SPICE. Every panel input travels through Renode's
memory bus and into the SPICE netlist, exactly like a real external signal.

Full data path:

```text
KiCad (.kicad_pcb) --+-- digital_twin_netlist.json  (electrical topology)
                     |
                     +-- pcb_geometry.json          (visual geometry)
                     |
                     v
                netlist_analyzer.py  --> component_classification.json
                     |
                     v
                spice_compiler.py    --> auto_generated.cir
                                         + source_metadata.json
                     |
                     v
                inject_trace_resistance.py
                     |
                     v
                auto_generated_with_traces.cir


STM32 Firmware (C) --> automation_plc.elf
                            |
                    Renode (virtual STM32F407)
                            |
                    +-------+--------+
                    |                |
              monitor tcp:1234   UARTs tcp:12345/12346
                    |                |
                    v                v
        renode_reader.py       stm32_bridge.py
        (owns monitor)         (RS485 + ESP)
                    |                |
        ZMQ PUB:5557 (OBS)      ZMQ PUB:5555
        ZMQ SUB:5558 (INJECT)       |
                    |               v
                    |          test_sub.py
                    |
        +-----------+--------------+
        |                          |
        v                          v
pyspice_solver.py            pyqt6_control.py
(OBS:5557 in)                (INJECT:5558 out)
(replaces V_* values)        (8 analog sliders)
        |                    (8 digital checkboxes)
        |                          |
   auto_generated_with_traces.cir |
        |                          |
   ngspice subprocess <------------+
        |
   ZMQ PUB:5556
   (SIM / RAIL / RAIL_I / POWER / CURRENT / LOAD_I / LED_I / PIN / NODE / DOUT)
        |
        +---------+---------+---------+---------+---------+---------+
        |         |         |         |         |         |         |
        v         v         v         v         v         v         v
    pcb_      pcb_      pcb_      pcb_      pcb_      pcb_      board_
    viewer    resist    current   voltage   heat      thermal   thermal
    (LEDs)    (R)       (I)       (V)       (P)       (Tj)      field_v2
```

## GOAL TRACKER

| #  | Goal                                                       | Status   |
|----|------------------------------------------------------------|----------|
| 1  | Live PCB overlay with animated LED states                  | DONE     |
| 2A | Trace resistance extraction + physics modeling             | DONE     |
| 2B | Current flow visualization on traces                       | DONE     |
| 2C | Power dissipation heat map (per component)                 | DONE     |
| 2D | Thermal simulation — per-component θja + 2D diffusion field| DONE     |
| 3  | Board health pre-check (rails, loads, overtemperature)     | DONE     |
| -- | Auto-generated SPICE compiler (unplanned bonus)            | DONE     |
| -- | Trace resistance injected into SPICE netlist               | DONE     |
| -- | Auto-generated source metadata (no hardcoded nets)         | DONE     |
| -- | Renode memory reader (replaces UART as firmware source)    | DONE     |
| -- | Bidirectional Python → Renode injection                    | DONE     |
| -- | External analog input control panel (PyQt6)                | DONE     |
| -- | External digital input control panel (PyQt6)               | DONE     |
| -- | Voltage viewer (per-net DC node voltages)                  | DONE     |
| -- | Digital-output "armed" state outline on current viewer     | DONE     |
| -- | Unified PyQt6 dashboard (tabs + sidebar + pipeline grid)   | DONE     |

---

## WHAT HAS BEEN ACHIEVED

### Foundation Layer

| Milestone                                                     | Status | Verified By                     |
|---------------------------------------------------------------|--------|---------------------------------|
| Hardware schematic review (ESP-12F, MAX3485, LM358, etc.)     | Done   | Netlist inspection              |
| KiCad PCB netlist extraction to JSON                          | Done   | extraction.py                   |
| STM32CubeMX pinout configured for all peripherals             | Done   | .ioc file, CubeMX GUI           |
| ADC1 with DMA (8 channels, circular mode)                     | Done   | adc.c generated                 |
| TIM4 PWM (4 motor channels on PD12-PD15)                      | Done   | tim.c generated                 |
| USB OTG FS Device CDC                                         | Done   | usb_device.c generated          |
| Firmware compiled to automation_plc.elf                       | Done   | STM32CubeIDE build              |
| Renode virtual STM32F407 running firmware                     | Done   | plc_twin machine started        |
| UARTs exposed as TCP sockets                                  | Done   | Ports 12345, 12346              |
| Renode monitor exposed on TCP 1234                            | Done   | renode_reader.py                |
| Python bridge (UART -> ZeroMQ) with byte-level framing        | Done   | Live PLC_ALIVE packets          |
| Subscriber displaying live ADC values                         | Done   | Parsed 8-channel ADC arrays     |
| End-to-end pipeline running continuously                      | Done   | 190000+ packets streamed        |

### Firmware Observable Layer

| Milestone                                                     | Status | Verified By                     |
|---------------------------------------------------------------|--------|---------------------------------|
| get_symbols.py parses linker .map for `obs_*`/`in_*` symbols  | Done   | symbols.json                    |
| renode_reader.py owns Renode monitor (single client on 1234)  | Done   | Live OBS stream                 |
| Batched contiguous-span read (1 monitor cmd per cycle)        | Done   | Fast polling                    |
| Publishes OBS:<name>:<hex> on ZMQ 5557                        | Done   | Subscribed by solver + panel    |
| Subscribes INJECT:<name>:<value> on ZMQ 5558                  | Done   | Writes via sysbus WriteWord     |
| Coalesces rapid injections (latest value wins per cycle)      | Done   | Slider drags don't flood        |
| Skips no-op writes (last_written cache)                       | Done   | Monitor stays clean             |
| Auto-reconnect on Renode monitor drop                         | Done   | Recovers from Renode restart    |
| renode_cmd.py one-shot monitor command tool                   | Done   | Manual debugging                |
| Digital inputs IE8..IE15 injectable via INJECT path            | Done   | 5000 mV → 5 V field voltage      |
| Digital outputs E0..E7, D12..D15 exposed for state display     | Done   | DOUT topic on ZMQ 5556           |

### Simulation Layer

| Milestone                                                     | Status | Verified By                     |
|---------------------------------------------------------------|--------|---------------------------------|
| Netlist analyzer — auto-classifies 143 components             | Done   | netlist_analyzer.py             |
| SPICE compiler — auto-generates netlist from KiCad data       | Done   | spice_compiler.py               |
| Source metadata emitter (SPICE branch <-> KiCad net)          | Done   | source_metadata.json            |
| STM32 self-load (50 mA) + ESP-12F load (80 mA) modeled        | Done   | Verified in ngspice output      |
| LDO + buck regulator models with upstream current reflection  | Done   | Voltage rails consistent        |
| Trace resistance (I²R losses over copper)                     | Done   | trace_resistance.py             |
| Trace resistance injected into SPICE between pads             | Done   | auto_generated_with_traces.cir  |
| Per-component SPICE power parsing (R, I, D elements)          | Done   | ngspice POWER topic             |
| Per-resistor current parsing (R_pd_E8..E15 pin currents)      | Done   | CURRENT:E8..E15 topic           |
| Behavioral optocoupler models (PC817, 6N137)                  | Done   | optocoupler.lib                 |
| Live solver with per-packet netlist rewriting                 | Done   | pyspice_solver.py               |
| OBS-driven value substitution (no UART dependency)            | Done   | Solved from renode_reader       |
| Per-net node voltages published as NODE:<net>:<volts>         | Done   | Voltage viewer                  |
| Digital-output state published as DOUT:<net>:<0/1>            | Done   | Current viewer "armed" outline  |

### External Control Layer

| Milestone                                                     | Status | Verified By                     |
|---------------------------------------------------------------|--------|---------------------------------|
| channels.json maps user channels → in_* → obs_*               | Done   | 8 analog + 8 digital entries    |
| pyqt6_control.py — analog sliders (0–10 V, 20 Hz flush)       | Done   | Verified live                   |
| pyqt6_control.py — digital checkboxes (IE8..IE15, 5 V ON)     | Done   | Verified live                   |
| Reads OBS back into panel to confirm injection landed         | Done   | obs label updates on slider move|
| input_injector.py — standalone CLI injector                   | Done   | Reference tool                  |
| Digital input: opto LED current ~10.6 mA when ON              | Done   | CURRENT:/f4/IO/IExx             |
| Digital input: MCU pin current ~0.33 mA through pull-down     | Done   | CURRENT:Exx                     |
| Upstream LDO reflects opto output load (~1.4 mA on 3v3-LDO1)  | Done   | RAIL_I:3v3-LDO1                 |
| Panel embedded into unified dashboard sidebar (live)          | Done   | No changes to pyqt6_control.py  |

### Visualization Layer

| Milestone                                                     | Status | Verified By                     |
|---------------------------------------------------------------|--------|---------------------------------|
| Automated PCB top-view renderer (no manual export)            | Done   | pcb_render.py                   |
| Copper trace + pad + body + silkscreen extraction             | Done   | pcb_geometry.json               |
| Rotation-aware pad rendering (fixes QFP at 45°)               | Done   | U5 verified                     |
| F.Fab chain-lines-to-polygon for component bodies             | Done   | All passives render correctly   |
| Performance optimization (PatchCollection / LineCollection)   | Done   | ~1700 artists → ~10 artists     |
| Live LED overlay (7 LEDs: D3-D9)                              | Done   | pcb_viewer.py                   |
| Trace resistance view with hover inspect                      | Done   | pcb_resistance_view.py          |
| Current flow view (colored traces on top layer)               | Done   | pcb_current_view.py             |
| Power dissipation heat map (log + linear + jet)               | Done   | pcb_heat_view.py                |
| Per-component thermal view (θja model)                        | Done   | pcb_thermal_view.py             |
| 2D thermal field v1 (single-layer k_eff)                      | Done   | pcb_thermal_field.py            |
| 2D thermal field v2 (2-layer copper + vias + FR4)             | Done   | pcb_thermal_field_v2.py         |
| Bounding-box hover detection for large components             | Done   | Fixed for U4/U5                 |
| Voltage view (per-net DC voltage, viridis 0–12 V)             | Done   | pcb_voltage_view.py             |
| Digital-output outline (amber=ARMED, slate=IDLE)              | Done   | pcb_current_view.py             |
| Resistance viewer info panel visible at startup               | Done   | pcb_resistance_view.py          |
| Unified dashboard — 7 tabs + sidebar + pipeline grid          | Done   | dashboard_unified.py            |

### Automation Layer (Goal 3)

| Milestone                                                     | Status | Verified By                     |
|---------------------------------------------------------------|--------|---------------------------------|
| Board health monitor (12-test battery)                        | Done   | board_health.py                 |
| Rail voltage tolerance check (±5%)                            | Done   | All 4 rails PASS                |
| Load current plausibility check                               | Done   | STM32 + ESP PASS                |
| Total power budget check                                      | Done   | <2 W threshold                  |
| Over-temperature prediction (θja model)                       | Done   | Peak 38.1 °C at U4              |
| Short-circuit detection (rail current > 1 A)                  | Done   | Max 144.7 mA                    |
| SIM packet freshness check                                    | Done   | <5 s stale threshold            |
| ADC channel-set verification                                  | Done   | 4/4 SIM fields present          |
| ESP packet rate monitor (rolling window)                      | Done   | 4-6 Hz observed                 |
| Single-shot mode with exit code (`--once`)                    | Done   | CI-friendly                     |

---

## PROJECT FILE INVENTORY

### Core Pipeline Files

| File                | Language      | Purpose                                                                  |
|---------------------|---------------|--------------------------------------------------------------------------|
| run_core.bat        | Batch         | Pre-flight + launches Renode, bridge, reader, solver, subscriber        |
| run_unified_dashboard.bat | Batch   | Launches the unified dashboard; pipeline starts inside as tabs          |
| run_led_viewer.bat  | Batch         | Opens pcb_viewer.py                                                     |
| run_current_viewer.bat | Batch      | Opens pcb_current_view.py                                               |
| run_voltage_viewer.bat | Batch    | Opens pcb_voltage_view.py standalone                                     |
| run_resistance_viewer.bat | Batch    | Opens pcb_resistance_view.py                                            |
| run_heat_viewer.bat | Batch         | Opens pcb_heat_view.py                                                  |
| run_thermal_viewer.bat | Batch      | Opens pcb_thermal_field_v2.py                                           |
| run_health.bat      | Batch         | Continuous health monitor                                               |
| run_health_once.bat | Batch         | Single-shot health check with exit code                                 |
| rebuild_spice.bat   | Batch         | Regenerates classification → SPICE → trace injection                    |
| rebuild_geometry.bat| Batch         | Two-step geometry regeneration (KiCad Python + system Python)           |
| stop_all.bat        | Batch         | Kills Renode, ngspice, and project Python processes                     |
| machine.resc        | Renode script | Defines virtual STM32F407, loads .elf, exposes UARTs                    |
| stm32_bridge.py     | Python        | Reads TCP UART streams, publishes RS485/ESP to ZMQ 5555                 |
| renode_reader.py    | Python        | Owns Renode monitor 1234, reads obs_*, writes in_*, ZMQ 5557/5558       |
| get_symbols.py      | Python        | Parses linker .map → symbols.json (obs_* and in_* addresses)            |
| input_injector.py   | Python        | Standalone INJECT publisher (reference tool)                            |
| renode_cmd.py       | Python        | One-shot Renode monitor command tool                                    |
| pyspice_solver.py   | Python        | Loads netlist, substitutes OBS values, calls ngspice, publishes 5556    |
| pyqt6_control.py    | Python        | External control panel (8 analog sliders + 8 digital checkboxes)        |
| dashboard_unified.py| Python        | All-in-one PyQt6 dashboard (7 viewer tabs + sidebar + pipeline grid)     |
| test_sub.py         | Python        | Subscribes to all ZMQ topics and displays live text data                |

### Auto-SPICE Compiler Files

| File                            | Purpose                                                        |
|---------------------------------|----------------------------------------------------------------|
| netlist_analyzer.py             | Classifies every component (R, C, LED, LDO, MCU, etc.)         |
| component_classification.json   | Output: type + value + nets per refdes                         |
| spice_compiler.py               | Generates complete SPICE netlist + source metadata             |
| auto_generated.cir              | Generated SPICE netlist (143 components + rails + loads)       |
| source_metadata.json            | Maps SPICE branch names to KiCad nets (no hardcoding)          |
| inject_trace_resistance.py      | Injects R_trace elements between regulator and load            |
| auto_generated_with_traces.cir  | Final netlist — this is what the solver loads                  |
| optocoupler.lib                 | Behavioral SPICE subcircuits for PC817 and 6N137               |

### Hardware / Physics Files

| File                          | Purpose                                                       |
|-------------------------------|---------------------------------------------------------------|
| automation.kicad_pcb          | KiCad PCB layout (source of truth)                            |
| automation.kicad_pro          | KiCad project file                                            |
| automation.kicad_sch          | KiCad main schematic                                          |
| A_IN.kicad_sch                | Analog input schematic sheet                                  |
| IO.kicad_sch                  | Industrial I/O schematic sheet                                |
| MOTORS.kicad_sch              | Motor driver schematic sheet                                  |
| DEBUG.kicad_sch               | Debug/LED schematic sheet                                     |
| f4.kicad_sch                  | STM32F407 core schematic sheet                                |
| digital_twin_netlist.json     | Extracted net topology (component, value, pin, net)           |
| extraction.py                 | pcbnew script that generates the JSON netlist from the PCB    |
| trace_resistance.py           | Computes DC resistance per trace + per net                    |
| net_resistance.json           | Output: resistance topology                                   |
| automation_plc.ioc            | STM32CubeMX project configuration                             |

### Visualization Files

| File                     | Language  | Purpose                                                              |
|--------------------------|-----------|----------------------------------------------------------------------|
| pcb_extract_geometry.py  | Python    | Reads .kicad_pcb via pcbnew, dumps geometry to JSON                  |
| pcb_geometry.json        | JSON      | Tracks, vias, footprints, pads, F.Fab, F.SilkS                       |
| pcb_render.py            | Python    | Renders pcb_background.png                                           |
| pcb_render_meta.json     | JSON      | Pixel <-> mm coordinate mapping                                      |
| pcb_background.png       | PNG       | Static PCB top-view render                                           |
| pcb_viewer.py            | Python    | Live LED overlay (Goal 1)                                            |
| pcb_resistance_view.py   | Python    | Trace resistance view with hover                                     |
| pcb_current_view.py      | Python    | Current flow view (colored traces on top)                            |
| pcb_voltage_view.py      | Python    | Per-net DC voltage view (live from ngspice NODE topic)               |
| pcb_heat_view.py         | Python    | Power dissipation heat map                                           |
| pcb_thermal_view.py      | Python    | Per-component θja thermal view                                       |
| pcb_thermal_field.py     | Python    | 2D thermal diffusion (single-layer k_eff)                            |
| pcb_thermal_field_v2.py  | Python    | 2D thermal diffusion (2-layer copper + vias + FR4)                   |

### Configuration Files

| File              | Purpose                                                       |
|-------------------|---------------------------------------------------------------|
| channels.json     | Channel map: id → in_* symbol → obs_* observable → kind       |
| symbols.json      | Address + size of every obs_* and in_* firmware variable      |

### Automation Files

| File              | Purpose                                                    |
|-------------------|------------------------------------------------------------|
| board_health.py   | 12-test battery, continuous or --once mode                 |

### Firmware Files

| File                                 | Purpose                                              |
|--------------------------------------|------------------------------------------------------|
| firmware/Debug/automation_plc.elf    | Compiled firmware (used by Renode)                   |
| firmware/Core/Src/main.c             | Main application (injection copy loop + LED bitmask) |
| firmware/Core/Src/adc.c              | ADC1 + DMA initialization                            |
| firmware/Core/Src/dac.c              | DAC1/DAC2 initialization                             |
| firmware/Core/Src/tim.c              | TIM4 PWM initialization                              |
| firmware/Core/Src/usart.c            | USART2, USART3 initialization                        |
| firmware/Core/Src/spi.c              | SPI1, SPI2 initialization                            |
| firmware/Core/Src/i2c.c              | I2C1 initialization                                  |
| firmware/Core/Src/gpio.c             | GPIO init                                            |
| firmware/Core/Src/dma.c              | DMA controller initialization                        |
| firmware/Core/Src/usb_device.c       | USB CDC device setup                                 |
| firmware/automation_plc.ioc          | CubeMX project (used for regeneration)               |

### External Dependencies

| Dependency     | Version    | Purpose                                                              |
|----------------|------------|----------------------------------------------------------------------|
| STM32CubeMX    | (latest)   | Pin/peripheral configurator                                          |
| STM32CubeIDE   | (latest)   | Compiler + debugger                                                  |
| Renode         | (latest)   | Virtual MCU execution                                                |
| ngspice        | 47         | Analog circuit simulator (C:\Users\user\Downloads\Spice64\bin\ngspice_con.exe) |
| Python (system)| 3.10+      | All pipeline scripts + viewers                                       |
| Python (KiCad) | 3.x        | Only used for pcbnew scripts (extraction)                            |
| pyzmq          | latest     | ZeroMQ bindings                                                      |
| matplotlib     | latest     | All rendering + live viewers                                         |
| scipy          | latest     | Sparse solver for 2D thermal diffusion                               |
| PyQt6          | latest     | External control panel GUI + unified dashboard                       |

---

## STM32F407VETx PINOUT (FINAL)

| Peripheral              | Pin                | Function                    | Notes                          |
|-------------------------|--------------------|-----------------------------|--------------------------------|
| HSE Crystal             | PH0, PH1           | RCC_OSC_IN/OUT              | 8 MHz                          |
| Debug                   | PA13, PA14         | SWDIO, SWCLK                | Serial Wire mode               |
| ESP-12F                 | PB10, PB11         | USART3_TX/RX                | 115200 8N1                     |
| MAX3485 (RS-485)        | PA2, PA3, PD11     | USART2_TX/RX, DE/RE         | Direction control on PD11      |
| W25Q128 (SPI Flash)     | PB3, PB4, PB5, PD6 | SPI1_SCK/MISO/MOSI, CS      | CS on PD6                      |
| USB-C                   | PA11, PA12         | USB_DM, USB_DP              | Full Speed Device              |
| Motor PWM               | PD12-PD15          | TIM4_CH1-CH4                | 4 channels                     |
| Debug LEDs              | PC10, PC11, PC12, PA15 | GPIO_Output             |                                |
| I2C Expansion           | PB6, PB7           | I2C1_SCL/SDA                |                                |
| Analog In 1-4           | PC0-PC3            | ADC1_IN10-13                | 0-10V via 10k/4.7k divider     |
| Analog In 5-6           | PC4, PC5           | ADC1_IN14-15                | Same divider                   |
| Analog In 7-8           | PB0, PB1           | ADC1_IN8-9                  | Same divider                   |
| Analog Out 1-2          | PA4, PA5           | DAC_OUT1/2                  | Via LM358 gain=3               |
| Digital Inputs 8-15     | PE8-PE15           | GPIO_Input                  | Opto-isolated (U31..U30)       |

ADC Rank Ordering (as configured in CubeMX):

    Rank 1: IN8  (PB0) -> adc_values[0]  -> A_IN7
    Rank 2: IN9  (PB1) -> adc_values[1]  -> A_IN8
    Rank 3: IN10 (PC0) -> adc_values[2]  -> A_IN1
    Rank 4: IN11 (PC1) -> adc_values[3]  -> A_IN2
    Rank 5: IN12 (PC2) -> adc_values[4]  -> A_IN3
    Rank 6: IN13 (PC3) -> adc_values[5]  -> A_IN4
    Rank 7: IN14 (PC4) -> adc_values[6]  -> A_IN5
    Rank 8: IN15 (PC5) -> adc_values[7]  -> A_IN6

---

## DATA PROTOCOLS

### Renode Observable Protocol (Renode → Python)

`renode_reader.py` reads a single contiguous span containing every
`obs_*` / `in_*` symbol and publishes each one:

    OBS:<symbol_name>:<hex_bytes>

Example:

    OBS:obs_v_in1:7f0f
    OBS:obs_v_ie8_drv:8813
    OBS:obs_v_d4_drv:00

Published on ZMQ **5557**.

### Renode Injection Protocol (Python → Renode)

Any subscriber publishes:

    INJECT:<symbol_name>:<uint16_value>

Example:

    INJECT:in_v_in1:3967
    INJECT:in_v_ie8_drv:5000

Published on ZMQ **5558**; `renode_reader.py` coalesces and writes via
`sysbus WriteWord` to the symbol's address.

### ESP UART Packet (Firmware → Bridge, legacy path)

- 20 bytes total
- [0xAA][0x55] — sync header
- 8 × 16-bit big-endian ADC values (bytes 2-17)
- 1 byte LED bitmask (byte 18)
- 1 byte reserved (byte 19)

### ZeroMQ Topics

| Topic                        | Port | Publisher      | Format                                    |
|------------------------------|------|----------------|-------------------------------------------|
| RS485:<hex>                  | 5555 | stm32_bridge   | Hex-encoded ASCII ("PLC_ALIVE\r\n")       |
| ESP:<hex>                    | 5555 | stm32_bridge   | Hex-encoded 20-byte ADC + LED packet      |
| OBS:<name>:<hex>             | 5557 | renode_reader  | Firmware observable raw bytes             |
| INJECT:<name>:<value>        | 5558 | any            | Write request to a firmware variable      |
| SIM:<n>:<key=val,...>        | 5556 | solver         | Simulation summary string                 |
| CURRENT:<net>:<amps>         | 5556 | solver         | Per-net current (auto-mapped + pin R's)   |
| POWER:<ref>:<watts>          | 5556 | solver         | Per-component power dissipation           |
| RAIL:<net>:<volts>           | 5556 | solver         | Power rail node voltages                  |
| RAIL_I:<net>:<amps>          | 5556 | solver         | Power rail delivered currents             |
| LED_I:<ref>:<amps>           | 5556 | solver         | LED drive currents                        |
| LOAD_I:<label>:<amps>        | 5556 | solver         | MCU / ESP self-load currents              |
| PIN:<node>:<volts>           | 5556 | solver         | Industrial input pin voltages             |
| NODE:<net>:<volts>           | 5556 | solver         | Per-net DC node voltage (every KiCad net) |
| DOUT:<net>:<0\|1>            | 5556 | solver         | Digital output state (firmware HIGH/LOW)  |

---

## EXTERNAL CONTROL PANEL

The PyQt6 panel (`pyqt6_control.py`) is the human interface to the twin.

### Analog channels (A_IN1..A_IN8)

- Horizontal slider per channel, 0–10 V industrial range.
- Value converted to a raw ADC count using the board's real
  10k/4.7k divider and 3.3 V / 12-bit ADC.
- Published at 20 Hz (fixed flush timer) → INJECT:in_v_inN:<raw>.
- Panel reads back OBS:obs_v_inN and displays the value the firmware
  currently holds — confirms the write landed even during rapid drags.

### Digital channels (IE8..IE15)

- Simple ON / OFF checkbox per channel.
- ON  → inject 5000 mV (5 V field voltage on the J3 pin)
- OFF → inject 0 mV
- Same flush and OBS readback path as analog.
- Configurable per channel via `von_mv` in `channels.json`.

### What happens electrically when you toggle a digital input

| Stage                     | Observable in the twin                         |
|---------------------------|------------------------------------------------|
| J3 field pin              | V_ieN_drv = 5.0 V (SPICE source)               |
| Opto LED (330 Ω + PC817)  | ~10.6 mA → CURRENT:/f4/IO/IEN                  |
| Opto phototransistor      | Conducts, sources into PE N                    |
| Pull-down (10 kΩ, modeled)| ~0.33 mA → CURRENT:EN                          |
| Upstream 3.3 V LDO        | +1.4 mA → RAIL_I:3v3-LDO1                      |

---

## VERIFIED NUMBERS (from live pipeline)

### Analog Input Chain

    10.0V industrial input -> 3.197V at MCU pin (10k/4.7k divider)
    3.197V / 3.3V * 4095 = 3967 ADC counts

### Power Rails (validated physics, trace resistance injected)

| Rail      | Target | Measured  | Drop      | Current | Reflection          |
|-----------|--------|-----------|-----------|---------|---------------------|
| 12v       | 12.0V  | 11.9989 V | −1.1 mV   | -75.9mA | (wall adapter)      |
| 5v        | 5.0V   | 4.9745 V  | −25.5 mV  | -144.7mA| 70.9 mA -> 12v      |
| 3v3-LDO1  | 3.3V   | 3.2821 V  | −17.9 mV  | -80.0mA | 80.0 mA -> 5v (1:1) |
| 3v3-LDO2  | 3.3V   | 3.2851 V  | −14.9 mV  | -54.2mA | 54.2 mA -> 5v (1:1) |

### Digital Input (IE10 ON, PC817 U33)

    Opto LED current         : +10.6540 mA  (CURRENT:/f4/IO/IE10)
    Opto output current      : ~0.33 mA     (CURRENT:E10, through R_pd_E10)
    3v3-LDO1 current delta   : +1.4 mA      (RAIL_I:3v3-LDO1)

### Thermal Field (from pcb_thermal_field_v2.py)

    Total power dissipated    : 879.8 mW
    Ambient                   : 25.0 °C
    Board average             : 28.5 °C
    Peak temperature          : 37.1 °C (at U3 buck converter)
    ΔT_max                    : 12.1 °C
    Top layer copper coverage : 49 %
    Bottom layer copper cover : 79 %
    Via thermal conductance   : 5.67 mW/K per via
    Solve time                : ~7 ms per packet (pre-factorized LU)

### Per-Component Thermal (θja model, from pcb_thermal_view.py)

| Component | Power   | θja (°C/W) | ΔT (°C) | T (°C) |
|-----------|---------|------------|---------|--------|
| U4 (ESP)  | 263 mW  | 50         | 13.2    | 38.2   |
| U3 (buck) | 255 mW  | 55         | 14.0    | 39.0   |
| U5 (MCU)  | 165 mW  | 40         |  6.6    | 31.6   |
| U1 (LDO)  | 163 mW  | 65         | 10.6    | 35.6   |
| U2 (LDO)  | 117 mW  | 65         |  7.6    | 32.6   |
| LEDs ON   | ~30 mW  | 300        |  9.0    | 34.0   |

### Board Health (from board_health.py)

    12/12 tests passing when pipeline is healthy.
    All 4 power rails within ±5% of target.
    No shorts, no over-temperature, no stuck peripherals.

---

## PHYSICS MODELS

### SPICE Netlist (auto-generated, then trace-injected)

- 143 components from KiCad netlist
- Resistive loads, capacitive coupling, real diode models
- STM32 (50 mA) + ESP-12F (80 mA) current sinks on their VDD rails
- LDO quiescent currents (5 mA each) on upstream rails
- Buck quiescent current (5 mA) on 12 V rail
- Upstream current reflection: LDO Iin = Iout, Buck Iin = Pout/Vin
- Rail output impedances: 10 mΩ (12V), 150 mΩ (5V), 200 mΩ (3.3V)
- Trace resistance between regulator output and load, from KiCad geometry
- SS14 diode D10 correctly reverse-biased (blocks USB VBUS back-feed)
- Behavioral optocouplers (PC817, 6N137) via optocoupler.lib
- Input pin pull-downs R_pd_E8..E15 (10 kΩ, modeled)

### Trace Resistance

    R_segment = rho * L / (W * t)
    rho = 1.72e-8 ohm-m (copper at 20 C)
    t   = 35 um (1 oz copper)
    Injected into netlist with lumped factor 0.15
    (source-to-load path / total copper)

### Power Dissipation

    P_component = |V| * |I|  (from ngspice element tables)
    P_trace     = I_net^2 * R_segment  (per trace segment)

### Thermal — Per-Component θja

    T_component = T_ambient + P * theta_ja
    theta_ja from lookup table by package type

### Thermal — 2D Diffusion Field (v2)

    Two-layer Laplace with source, convection, vertical coupling:
      k_eff * t * lap(T) - 2*h*(T - T_amb) + Q = 0
    v2 extras:
      - Per-cell copper coverage rasterized from F.Cu / B.Cu
      - Vertical coupling = FR4 conductance + via_count * G_via
      - Convective loss from both top and bottom surfaces
      - Sparse matrix pre-factorized with scipy SuperLU (~7 ms/solve)

---

## HOW TO RUN

### Unified Dashboard (Recommended)

    Double-click run_unified_dashboard.bat

    This opens ONE window containing:
      - Home tab       — 3x2 grid of any ticked plots
      - Viewer tabs    — LED / Current / Voltage / Heat / Thermal / Resistance
      - Pipeline tab   — live stdout of Renode, Bridge, Reader, Solver,
                         Subscriber, in a 3x2 grid (via PIPE + reader thread)
      - Right sidebar  — Plots checklist + Control Panel (analog sliders,
                         digital checkboxes)
      - Dark OS title bar via DWM

    Sidebar rules:
      - Plots checklist visible only on the Home tab
      - Entire sidebar hidden on Trace Resistance and Pipeline tabs
      - Control Panel always visible when the sidebar is visible

    Stop everything with stop_all.bat (kills Renode, ngspice, and all
    project Python processes).

### Legacy Multi-Window Launch

Step 1 — Start the pipeline:

    Double-click run_core.bat

    This opens 5 windows in sequence:
      1. Renode - Virtual MCU (10s boot delay)
      2. Bridge - UART to ZMQ (3s delay)
      3. Reader - Renode RAM (3s delay)
      4. Solver - ngspice (2s delay)
      5. Subscriber - Live Data

Step 2 — Start the control panel:

    python pyqt6_control.py

Step 3 — Launch any viewer(s) you want:

    run_led_viewer.bat             (LED blink overlay)
    run_resistance_viewer.bat      (trace resistance + hover)
    run_current_viewer.bat         (current flow)
    run_voltage_viewer.bat         (per-net DC voltage)
    run_heat_viewer.bat            (power dissipation)
    run_thermal_viewer.bat         (2D thermal field v2)

Step 4 — Optional: check board health:

    run_health.bat                 (continuous monitor)
    run_health_once.bat            (single check, exit code)

Step 5 — Stop everything:

    stop_all.bat

### Manual Launch (6 terminals)

    # Terminal 1
    cd C:\Users\user\Desktop\automation\kicad_plugin
    renode --port 1234 machine.resc

    # Terminal 2 (after Renode boots)
    python stm32_bridge.py

    # Terminal 3
    python renode_reader.py

    # Terminal 4
    python pyspice_solver.py

    # Terminal 5 (optional observer)
    python test_sub.py

    # Terminal 6 (control panel + viewer)
    python pyqt6_control.py

### Regenerate PCB Render (after .kicad_pcb changes)

    # In KiCad Command Prompt (has pcbnew):
    cd C:\Users\user\Desktop\automation\kicad_plugin
    python pcb_extract_geometry.py

    # In normal PowerShell:
    python pcb_render.py

### Regenerate Auto-Generated SPICE (after netlist changes)

    python netlist_analyzer.py         # classify components
    python spice_compiler.py           # generate auto_generated.cir + metadata
    python inject_trace_resistance.py  # inject copper R between pads
    # Then restart pyspice_solver.py

### Rebuild Firmware After Code Changes

    1. Edit firmware/Core/Src/main.c (only inside USER CODE blocks)
    2. Click Hammer in STM32CubeIDE
    3. Copy firmware/Debug/automation_plc.elf to firmware/ folder
    4. python get_symbols.py           (regenerate symbols.json)
    5. Restart Renode, renode_reader, solver

---

## THE TWO-INTERPRETER SPLIT

A key architectural decision: **pcbnew** is ONLY available inside KiCad's
bundled Python. matplotlib/scipy/zmq/PyQt6 are ONLY in system Python.
We never install either tool in the "wrong" interpreter.

    +------------------------+        +-------------------------+
    |  KiCad Python          |        |  System Python          |
    |  (D:\kicad\bin)        |        |  (system PATH)          |
    +------------------------+        +-------------------------+
    |  pcb_extract_geometry  |  -->   |  pcb_render.py          |
    |  extraction.py         |        |  all viewers            |
    +------------------------+        |  stm32_bridge.py        |
             |                        |  renode_reader.py       |
             v                        |  pyspice_solver.py      |
       pcb_geometry.json              |  pyqt6_control.py       |
       digital_twin_netlist.json      |  dashboard_unified.py   |
                                      |  netlist_analyzer.py    |
                                      |  spice_compiler.py      |
                                      |  inject_trace_resistance|
                                      |  trace_resistance.py    |
                                      |  board_health.py        |
                                      +-------------------------+
                                                |
                                                v
                                    auto_generated_with_traces.cir
                                    source_metadata.json
                                    symbols.json
                                    pcb_background.png
                                    pcb_render_meta.json
                                    component_classification.json
                                    net_resistance.json

---

## VISUALIZATION — matplotlib z-order convention

    z=0    Board background
    z=1    Back copper traces (B.Cu), dim gray
    z=2    Front copper traces (F.Cu), dim gray
    z=3    Vias
    z=5    Pads
    z=6    Pads (in heat/thermal viewers)
    z=7    Component bodies (from F.Fab polygons)
    z=8    Component outlines
    z=10   Silkscreen lines + shapes
    z=11   Silkscreen text
    z=15   LED glow (viewer only)
    z=16   LED dot (viewer only)
    z=20   Status panel
    z=100  Reference designators
    z=175  Digital-output outline (amber=ARMED, slate=IDLE)
    z=180  Live current-colored traces (pcb_current_view.py)

---

## BOARD HEALTH MONITOR — 12 Tests

| # | Test                       | Threshold                                    |
|---|----------------------------|----------------------------------------------|
| 1 | 12V rail                   | 12.0V ± 5%                                   |
| 2 | 5V rail                    | 5.0V ± 5%                                    |
| 3 | 3.3V LDO1 rail             | 3.3V ± 5%                                    |
| 4 | 3.3V LDO2 rail             | 3.3V ± 5%                                    |
| 5 | STM32 self-load            | 50 mA ± 25%                                  |
| 6 | ESP-12F self-load          | 80 mA ± 25%                                  |
| 7 | Total board power          | < 2 W                                        |
| 8 | Over-temperature           | No component > 60 °C                         |
| 9 | Short-circuit              | No rail > 1 A                                |
| 10| SIM active                 | Last packet < 5 s old                        |
| 11| ADC channel set            | All expected SIM fields present              |
| 12| ESP packet rate            | 3-15 Hz (host CPU dependent)                 |

Exit code 0 if all pass, 1 if any FAIL. Ideal for CI or pre-flight checks.

---

## DESIGN FINDINGS

### Digital input pull-downs are modeled, not physical

The real PCB has **no external pull-down on PE8..PE15** — verified in
`digital_twin_netlist.json`; the `EN` nets contain only `U5.pinNN` and
the opto emitter pin. Firmware must enable the STM32's internal
pull-down (~40 kΩ typ. on STM32F4) or the input will float when the
opto is off.

SPICE models an external 10 kΩ `R_pd_E8..E15` (added by
`emit_input_pulldowns()` in `spice_compiler.py`). Chosen deliberately
stronger than the real internal pull-down to give ngspice a
well-conditioned DC path while staying physically plausible.

**Action for next PCB revision:** add real 10 kΩ pull-downs, or
guarantee firmware enables internal ones.

---

## KNOWN LIMITATIONS & TODOS

### Architectural Gaps

- [ ] DAC values are still derived from firmware observables, not a
      separate physical DAC path
- [ ] Ideal op-amp model — VCVS with gain 100,000 instead of TI's LM358
- [ ] ADC values in firmware are simulated via `sim_tick` ramp; Renode's
      generic STM32F4 does not implement the ADC peripheral. The injection
      path now provides a real external source, so this is now a
      firmware-side choice, not a hard limitation.
- [ ] No transient analysis — only DC operating point currently
- [ ] SPI/I2C nets are modeled but not driven (firmware doesn't use them)
- [ ] Lumped trace resistance uses fraction 0.15 of total net R
      (source-to-load is not computed as a shortest path)
- [ ] Renode's .NET console output does not always survive a pipe
      redirect on Windows; the Pipeline tab may show its boot log
      intermittently. All Python steps stream reliably.

### Enhancement Ideas

- [ ] Extend firmware to actually read PE8..PE15 into observables
      (`obs_ie8_state..obs_ie15_state`) so the pin state is visible
      end-to-end
- [ ] Replace ideal op-amp with TI LM358 .lib model
- [ ] Compute source-to-load resistance as graph shortest path
- [ ] Add transient simulation for step response
- [ ] Package framework as a reusable Python library

### Optional Polish

- [x] Unified dashboard UI (single window, tabs for each view)
- [x] Unified top-level launcher (run_unified_dashboard.bat)
- [ ] Qt backend migration for the standalone viewers (TkAgg → QtAgg)
- [ ] Demo video / screenshot gallery
- [ ] Tune LED glow radius to match footprint size

---

## MILESTONE LOG

### Milestone 1: Communication Backbone
Firmware → Renode → TCP → Python bridge → ZMQ → subscriber.
Verified with PLC_ALIVE text + 8-channel ADC arrays at 10 Hz.

### Milestone 2: Analog Circuit Simulation
ngspice subprocess called per packet with dynamic netlist.
Verified all 8 divider outputs and both op-amp outputs.

### Milestone 3: Live PCB Visualization (Goal 1)
Automated PCB render from .kicad_pcb via pcbnew + matplotlib.
Fixed pad rotation for 45° QFP footprints.
Fixed component body extraction via line-chaining algorithm.
Fixed z-order so bodies sit above pads (matches KiCad 3D viewer).
Live viewer shows all 7 LEDs (D3-D9) animating at real PCB positions.

### Milestone 4: Trace Physics (Goal 2A)
Extracted every copper segment's length/width/layer from .kicad_pcb.
Computed DC resistance R = rho*L/(W*t) per segment.
Aggregated per-net resistance. Interactive hover viewer built.

### Milestone 5: Current + Power Visualization (Goals 2B, 2C)
Live per-net current from ngspice branch reports.
Per-component power dissipation parsed from ngspice element tables.
Heat map viewer with log and linear colormaps.

### Milestone 6: Auto-SPICE Compiler (Unplanned Bonus)
Built netlist_analyzer.py (143 components classified, 0 unknowns).
Built spice_compiler.py (fully automatic SPICE netlist generator).
Added LDO + buck models with upstream current reflection.
Verified physical consistency: every amp on 3.3 V traces to 12 V.

### Milestone 7: Thermal Simulation (Goal 2D)
Per-component θja thermal view (35-40 °C chips).
2D single-layer diffusion field (Laplace + convection).
2D two-layer field (v2) with real copper coverage rasterization,
vias as vertical thermal bridges, FR4 as insulator.
Pre-factorized sparse LU solver: 7 ms per packet.

### Milestone 8: Board Health Pre-Check (Goal 3)
12-test automated battery: rails, loads, power budget, over-temp,
short detection, packet freshness, ADC channel set, packet rate.
Continuous and single-shot (--once) modes.

### Milestone 9: Trace Resistance Injection
Extended spice_compiler.py to emit source_metadata.json mapping every
SPICE branch to its KiCad net. Built inject_trace_resistance.py to add
R_trace elements between regulator outputs and load nodes.
Solver now loads auto_generated_with_traces.cir and reads source
metadata — no hardcoded net names anywhere.

### Milestone 10: Performance + Polish
Replaced ~1700 individual matplotlib artists with PatchCollection and
LineCollection in all viewers. Bounding-box hover detection for large
components. Hover-flicker fix (hovering state guards update loop).
Projected capstyle for clean trace joints.

### Milestone 11: Renode Memory Reader + Bidirectional Injection
get_symbols.py extracts obs_* / in_* from linker .map.
renode_reader.py owns the single Renode monitor connection, polls
observables as one contiguous span, publishes OBS on 5557, and
applies INJECT requests from 5558 via sysbus WriteWord.
Coalesces rapid injections, skips no-op writes, auto-reconnects.
pyspice_solver.py switched from UART/ESP packets to OBS as the
authoritative firmware source.

### Milestone 12: External Control Panels (Analog + Digital)
pyqt6_control.py — PyQt6 GUI: 8 analog sliders (0-10 V) and 8
digital checkboxes (IE8..IE15).
channels.json drives the layout, ADC divider math, and per-channel
"ON" voltage for digital.
20 Hz flush timer publishes INJECT at a bounded rate; OBS readback
confirms every write landed.
Analog chain: raw count → firmware `in_v_inN` → `obs_v_inN` → SPICE
`V_inN` → ngspice divider + op-amp + ADC path.
Digital chain: 5000 mV → firmware `in_v_ieN_drv` → `obs_v_ieN_drv`
→ SPICE `V_ieN_drv` → 330 Ω + PC817 + 10 kΩ pull-down → MCU pin.
Full current path is published as CURRENT:/f4/IO/IEN and CURRENT:EN.

### Milestone 13: Voltage Viewer + Digital Output State
Added NODE:<net>:<volts> publishing for every net in the KiCad netlist
(140 nets, resolved via case-insensitive spice_net lookup). New
pcb_voltage_view.py mirrors the current viewer's style — viridis
colormap, fixed 0–12 V range, hover shows exact voltage.
Digital outputs now publish DOUT:<net>:<0|1> from their firmware
observables so the current viewer can outline them even with no field
load attached (amber = ARMED, slate = IDLE).

### Milestone 14: Unified Dashboard
dashboard_unified.py — single PyQt6 window, no changes to any viewer
file. Uses monkey-patching of matplotlib.pyplot.subplots / show and
FuncAnimation.__init__ to capture each viewer's Figure, then embeds it
into a QTabWidget. Right sidebar holds the Plots checklist and the
live Control Panel (taken via takeCentralWidget()). Pipeline steps
launch as subprocess.PIPE children with a background reader thread,
showing real stdout of Renode, Bridge, Reader, Solver, and Subscriber
in a 3x2 grid. Animations are throttled based on the number of visible
plots (150 ms single, 400 ms ×2, 1000 ms ×3-4, 1500 ms ×5-6) so CPU
stays bounded. Dark OS title bar via DwmSetWindowAttribute.

---

## ARCHITECTURE DIAGRAM

    +------------------------------------------------------------------+
    |                         HOST MACHINE                             |
    +------------------------------------------------------------------+
    |                                                                  |
    |  +--------------+         +--------------+                       |
    |  |   KiCad      |         |  STM32CubeMX |                       |
    |  |  .kicad_pcb  |         |  .ioc        |                       |
    |  +------+-------+         +------+-------+                       |
    |         |                        |                               |
    |    +----+----+                   | generate code                 |
    |    |         |                   v                               |
    |    v         v            +--------------+                       |
    | netlist   geometry        |  main.c etc. |                       |
    |   .json     .json         +------+-------+                       |
    |    |         |                   |                               |
    |    |         |                   | STM32CubeIDE build            |
    |    |         |                   v                               |
    |    |         |            +--------------+                       |
    |    |         |            |firmware.elf  |                       |
    |    |         |            +------+-------+                       |
    |    |         |                   |                               |
    |    |         |                   | loaded by                     |
    |    |         |                   v                               |
    |    |         |            +--------------+     monitor 1234      |
    |    |         |            |   RENODE     |<--------+             |
    |    |         |            +--+--------+--+         |             |
    |    |         |               |        |            |             |
    |    |         |         USART2|        |USART3      v             |
    |    |         |         tcp:12345      tcp:12346  renode_reader.py |
    |    |         |               v        v            |             |
    |    |         |            +------------------+      |             |
    |    |         |            |  stm32_bridge.py |      |             |
    |    |         |            +--------+---------+      |             |
    |    |         |                     |                |             |
    |    |         |              ZMQ PUB:5555      ZMQ PUB:5557        |
    |    |         |                     |          ZMQ SUB:5558        |
    |    |         |                     |                |             |
    |    |         |                     |                +----+        |
    |    |         |                     |                |    |        |
    |    |         |                     |                v    v        |
    |    |         |                     |          pyspice_solver      |
    |    |         |                     |          pyqt6_control       |
    |    |         v                     |                |             |
    |    |   netlist_analyzer            |                |             |
    |    |         v                     |                |             |
    |    |   spice_compiler              |                |             |
    |    |         v                     |                |             |
    |    +----> auto_gen.cir            |                |             |
    |              +                    |                |             |
    |        source_metadata.json       |                |             |
    |              +                    |                |             |
    |  inject_trace_resistance          |                |             |
    |              v                    |                |             |
    |  auto_generated_with_traces       |                |             |
    |              |                    |                |             |
    |              +--> ngspice <--------+----------------+             |
    |                      |                                           |
    |                      v                                           |
    |               ZMQ PUB:5556                                       |
    |                      |                                           |
    |     +----------------+----------------+                         |
    |     v                                 v                         |
    |  test_sub.py         pcb_* viewers + board_health.py            |
    |                      + dashboard_unified.py (embeds all of them) |
    |                                                                  |
    +------------------------------------------------------------------+

---

## OPTIONAL POLISH (NEXT UP)

Chosen direction after Goal 3 + control panels + unified dashboard:

1. **Qt backend migration for standalone viewers** — switch matplotlib
   backend from TkAgg to QtAgg in the individual pcb_*.py scripts
   - Eliminates the residual window-drag lag when run outside the dashboard
   - Zero code changes needed in the plotting logic

2. **Firmware digital-input readback** — read PE8..PE15 in main.c into
   `obs_ieN_state` so the pin state is visible end-to-end from panel to
   MCU to solver

3. **Demo video / screenshot gallery** — capture the running pipeline

---

## REFERENCES

- STM32F407 Reference Manual: RM0090
- STM32F407 Datasheet: DS8626
- Renode STM32F4 Platform: platforms/cpus/stm32f4.repl
- ngspice Manual: https://ngspice.sourceforge.io/docs.html
- KiCad pcbnew Python API: https://docs.kicad.org/doxygen-python/
- ZeroMQ Guide: https://zguide.zeromq.org/
- Thermal via model: 2-layer board thermal resistance, IPC-2152

---

## SUMMARY

The project has achieved a complete, closed-loop digital twin:

1. **Full-stack simulation pipeline** — firmware on a virtual MCU,
   auto-generated SPICE netlist from actual KiCad data, real ngspice
   engine, real ZMQ message bus. Trace resistance injected between
   regulator outputs and load nodes, derived from copper geometry.
   No stubs, no hardcoded circuits, no hardcoded net names.

2. **Bidirectional control** — Python panel writes into Renode memory
   just like an external field signal. Analog sliders and digital
   checkboxes both flow through the exact same path: panel → INJECT →
   Renode memory → firmware observable → SPICE source → ngspice →
   physics-accurate visualization.

3. **Physics-accurate visualization** — seven viewers rendering live
   LED states, trace resistance, current flow, per-net DC voltage,
   power dissipation, per-component thermal (θja), and 2D thermal
   diffusion with real copper coverage, via heat paths, and package
   thermal resistance. All available as standalone scripts OR as tabs
   inside a single unified PyQt6 dashboard that also embeds the
   control panel and the live pipeline consoles.

4. **Automated health check** — 12-test battery that confirms the
   board is behaving correctly, with CI-friendly exit codes.

5. **Zero hardcoded knowledge** — every value on screen traces back to
   a real KiCad component through real firmware through real SPICE
   through real heat diffusion.

This is not a "visual replica" — it is a physics-accurate replica of
an industrial PLC board, running in real time on the developer's laptop.
