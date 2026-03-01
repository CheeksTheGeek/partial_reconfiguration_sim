`timescale 1ns/1ps

//============================================================================
// echo_counter_rm.sv
//
// Reconfigurable module for verifying cycle accuracy in DPI-based
// partial reconfiguration simulation.
//
// This RM has its own independent counter and echoes back the static region's
// counter value. In a cycle-accurate simulation (shared memory + barrier sync):
//   - rm_counter and static_counter_in should differ by a CONSTANT offset
//   - latency_measurement should be CONSTANT
//============================================================================

module echo_counter_rm (
    input  wire        clk,
    input  wire [31:0] static_counter_in,     // Counter value from static region
    output reg  [31:0] rm_counter,            // RM's own independent counter
    output reg  [31:0] static_counter_echo,   // Echo back the received static counter
    output reg  [31:0] latency_measurement    // Difference: rm_counter - static_counter_in
);

    //------------------------------------------------------------------------
    // RM's Independent Counter
    //
    // This counter increments every clock cycle, just like the activity_counter
    // in the static region. In a cycle-accurate simulation, both counters
    // should increment in lockstep (possibly with a fixed offset due to
    // pipeline delay).
    //------------------------------------------------------------------------
    always @(posedge clk) begin
        // Increment RM's own counter every cycle
        rm_counter <= rm_counter + 1;
    end

    //------------------------------------------------------------------------
    // Static Counter Echo
    //
    // Echo back the static counter value we received. This allows Python to
    // measure round-trip latency: the difference between the current static
    // counter and the echoed value represents the communication delay.
    //------------------------------------------------------------------------
    always @(posedge clk) begin
        static_counter_echo <= static_counter_in;
    end

    //------------------------------------------------------------------------
    // Latency Measurement
    //
    // Calculate the instantaneous difference between our counter and the
    // static counter we received. In a cycle-accurate system, this should
    // be a CONSTANT value (the pipeline depth).
    //
    // With barrier-synchronized shared memory, this should be constant
    // (cycle-accurate). The DPI double-buffer swap ensures data arrives
    // exactly one cycle after it was written.
    //------------------------------------------------------------------------
    always @(posedge clk) begin
        latency_measurement <= rm_counter - static_counter_in;
    end

endmodule
