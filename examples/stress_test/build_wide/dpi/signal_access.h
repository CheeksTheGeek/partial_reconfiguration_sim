#ifndef SIGNAL_ACCESS_H
#define SIGNAL_ACCESS_H

#include <cstdint>
#include <verilated.h>
#include "dpi_shm_channel.h"

#include "Vwide_static.h"

// Global partition bases (defined in static_driver.cpp)
extern void* g_partition_bases[];

inline uint64_t read_static_port(Vwide_static* model, int port_idx) {
    switch (port_idx) {
        case 0: return static_cast<uint64_t>(model->tick);
        default: return 0;
    }
}

inline void write_static_port(Vwide_static* model, int port_idx, uint64_t value) {
    switch (port_idx) {
        default: break;
    }
}

inline uint64_t read_rp_wide64_port(int port_idx) {
    void* base = g_partition_bases[0];
    ShmPartitionHeader* hdr = shm_header(base);
    switch (port_idx) {
        case 0: {
            ShmPortOverride* ovr = shm_to_rm_override(base, 0);
            if (shm_load32(&ovr->active)) return shm_load64_relaxed(&ovr->value);
            return shm_load64_relaxed(&shm_to_rm_inbox(base, 0)->data);
        }
        case 1: {
            ShmPortOverride* ovr = shm_to_rm_override(base, 1);
            if (shm_load32(&ovr->active)) return shm_load64_relaxed(&ovr->value);
            return shm_load64_relaxed(&shm_to_rm_inbox(base, 1)->data);
        }
        case 2: return shm_load64_relaxed(&shm_from_rm_outbox(base, hdr->num_to_rm, 0)->data);
        case 3: return shm_load64_relaxed(&shm_from_rm_outbox(base, hdr->num_to_rm, 1)->data);
        default: return 0;
    }
}

inline void write_rp_wide64_port(int port_idx, uint64_t value) {
    void* base = g_partition_bases[0];
    switch (port_idx) {
        case 0: {
            ShmPortOverride* ovr = shm_to_rm_override(base, 0);
            shm_store64_relaxed(&ovr->value, value);
            shm_store32(&ovr->active, 1);
            break;
        }
        case 1: {
            ShmPortOverride* ovr = shm_to_rm_override(base, 1);
            shm_store64_relaxed(&ovr->value, value);
            shm_store32(&ovr->active, 1);
            break;
        }
        default: break;
    }
}

#endif // SIGNAL_ACCESS_H