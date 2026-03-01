#ifndef SHM_MAILBOX_H
#define SHM_MAILBOX_H

#include <cstdint>

#define SHM_MAILBOX_SIZE 4096

// Command codes
#define CMD_NOOP     0
#define CMD_READ     1
#define CMD_WRITE    2
#define CMD_RECONFIG 3
#define CMD_QUIT     0xFF

// Simulation status
#define SIM_STATUS_INIT    0
#define SIM_STATUS_RUNNING 1
#define SIM_STATUS_DONE    2
#define SIM_STATUS_ERROR   3

// Target codes for read/write
#define TARGET_STATIC 0
// Partition targets: 1, 2, 3, ... (1-based)

struct ShmMailbox {
    volatile uint32_t sim_status;     // SIM_STATUS_*
    uint32_t _pad0;
    volatile uint64_t cycle_count;    // Current cycle count
    volatile uint32_t cmd;            // CMD_*
    volatile uint32_t target;         // TARGET_STATIC or partition index (1-based)
    volatile uint32_t port_idx;       // Port index within target
    volatile uint32_t rm_idx;         // RM index for reconfig
    volatile uint64_t write_value;    // Value for write commands
    volatile uint64_t read_value;     // Value from read commands
};

#endif // SHM_MAILBOX_H