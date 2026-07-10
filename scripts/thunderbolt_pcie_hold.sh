#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: thunderbolt_pcie_hold.sh [--rescan] [--detect]

Keep the NiteFury Thunderbolt PCIe path awake on Linux by forcing the
Thunderbolt root ports, bridge chips, NHI controllers, and any Xilinx endpoint
to runtime power "on" with D3cold disabled.

Options:
  --rescan   Rescan held PCI bridges after applying the power hold.
  --detect   Run openFPGALoader JTAG detection after applying the hold.

Environment:
  DAU_TB_PCI_DEVICES  Space-separated PCI BDFs to hold instead of auto-detecting.
  DAU_JTAG_CABLE      openFPGALoader cable name, default: digilent_hs2.
USAGE
}

rescan=false
detect=false

for argument in "$@"; do
    case "$argument" in
        --rescan)
            rescan=true
            ;;
        --detect)
            detect=true
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "error: unknown argument: $argument" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo "error: run as root so sysfs power controls can be written" >&2
    exit 1
fi

declare -A selected_devices=()

add_device() {
    local device_id=$1

    [[ -n "$device_id" ]] || return 0
    [[ -d "/sys/bus/pci/devices/$device_id" ]] || return 0
    selected_devices["$device_id"]=1
}

add_parent_chain() {
    local device_id=$1
    local sysfs_path
    local parent_path
    local parent_id

    sysfs_path="/sys/bus/pci/devices/$device_id"
    [[ -e "$sysfs_path" ]] || return 0

    parent_path=$(readlink -f "$sysfs_path/..")
    while [[ "$parent_path" == /sys/devices/* ]]; do
        parent_id=$(basename "$parent_path")
        if [[ -d "/sys/bus/pci/devices/$parent_id" ]]; then
            selected_devices["$parent_id"]=1
        fi
        parent_path=$(dirname "$parent_path")
    done
}

if [[ -n "${DAU_TB_PCI_DEVICES:-}" ]]; then
    for device_id in ${DAU_TB_PCI_DEVICES}; do
        add_device "$device_id"
        add_parent_chain "$device_id"
    done
else
    while read -r device_id; do
        add_device "$device_id"
        add_parent_chain "$device_id"
    done < <(lspci -Dnn | awk '/Thunderbolt|JHL|10ee:7011|Xilinx/ { print $1 }')
fi

if [[ ${#selected_devices[@]} -eq 0 ]]; then
    echo "warning: no Thunderbolt/Xilinx PCI devices found; try running lspci once or set DAU_TB_PCI_DEVICES" >&2
fi

echo "Applying runtime PM hold"
for device_id in $(printf '%s\n' "${!selected_devices[@]}" | sort); do
    sysfs_path="/sys/bus/pci/devices/$device_id"
    [[ -d "$sysfs_path" ]] || continue

    echo "-- $device_id"
    if [[ -w "$sysfs_path/power/control" ]]; then
        echo on > "$sysfs_path/power/control"
    fi
    if [[ -w "$sysfs_path/d3cold_allowed" ]]; then
        echo 0 > "$sysfs_path/d3cold_allowed"
    fi

    for state_file in power/control power/runtime_status d3cold_allowed current_link_speed current_link_width; do
        if [[ -e "$sysfs_path/$state_file" ]]; then
            printf '  %s=' "$state_file"
            cat "$sysfs_path/$state_file" || true
        fi
    done
done

if [[ "$rescan" == true ]]; then
    echo "Rescanning held PCI bridges"
    for device_id in $(printf '%s\n' "${!selected_devices[@]}" | sort -r); do
        sysfs_path="/sys/bus/pci/devices/$device_id"
        if [[ -r "$sysfs_path/class" ]] && [[ $(cat "$sysfs_path/class") == 0x0604* ]] && [[ -w "$sysfs_path/rescan" ]]; then
            echo "-- rescan $device_id"
            echo 1 > "$sysfs_path/rescan" || true
        fi
    done
fi

echo "PCIe summary"
lspci -Dnn | grep -Ei 'Thunderbolt|JHL|10ee:7011|Xilinx' || true

if [[ "$detect" == true ]]; then
    echo "JTAG detect"
    openFPGALoader -c "${DAU_JTAG_CABLE:-digilent_hs2}" --detect
fi