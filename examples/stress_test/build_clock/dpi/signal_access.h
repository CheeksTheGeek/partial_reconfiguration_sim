#ifndef SIGNAL_ACCESS_H
#define SIGNAL_ACCESS_H

#include <cstdint>
#include <verilated.h>
#include "dpi_shm_channel.h"

#include "Vweird_clock_static.h"

// Global partition bases (defined in static_driver.cpp)
extern void* g_partition_bases[];

inline uint64_t read_static_port(Vweird_clock_static* model, int port_idx) {
    switch (port_idx) {
        case 0: return static_cast<uint64_t>(model->sys_input);
        case 1: return static_cast<uint64_t>(model->counter);
        default: return 0;
    }
}

inline void write_static_port(Vweird_clock_static* model, int port_idx, uint64_t value) {
    switch (port_idx) {
        case 0: model->sys_input = static_cast<uint32_t>(value); break;
        default: break;
    }
}

inline uint64_t read_rp_dummy_port(int port_idx) {
    void* base = g_partition_bases[0];
    ShmPartitionHeader* hdr = shm_header(base);
    switch (port_idx) {
        case 0: return shm_load64_relaxed(&shm_from_rm_outbox(base, hdr->num_to_rm, 0)->data);
        default: return 0;
    }
}

inline void write_rp_dummy_port(int port_idx, uint64_t value) {
    void* base = g_partition_bases[0];
    switch (port_idx) {
        default: break;
    }
}

#endif // SIGNAL_ACCESS_H