---
layout: default
title: Digital IC Synthesis
parent: Case Studies
nav_order: 4
---

# Digital IC Synthesis

*Case study coming soon.*

This page is the planned landing for an RTL-to-GDS flow case study using the [OpenROAD](https://theopenroadproject.org/) and [iic-osic-tools](https://github.com/iic-jku/iic-osic-tools) services.

The intended workflow follows the standard digital backend:

1. **Synthesis** — Yosys reads the RTL and emits a gate-level netlist against a target standard-cell library.
2. **Floorplanning + place-and-route** — OpenROAD takes the synthesized netlist through floorplan, global+detailed placement, clock-tree synthesis, and global+detailed routing.
3. **Sign-off** — DRC and LVS via the open-source PDK tools bundled in `iic-osic-tools`; STA via OpenSTA.
4. **GDS export** — final layout written to GDSII.

Both `openroad` and `iic-osic-tools` are registered in `src/sciagent/services/registry.yaml`. The `iic-osic-tools` image carries 80+ open-source IC design tools — Yosys, OpenROAD, OpenSTA, KLayout, Magic, Netgen, Xschem, ngspice, and more — so a single container covers the whole flow.

When the case study lands it will demonstrate end-to-end synthesis of a small open-source RTL design (likely a RISC-V core) with a published PDK, validation against expected timing/area numbers, and the resulting GDS opened in KLayout.
