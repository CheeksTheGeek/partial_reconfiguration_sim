`timescale 1ns/1ps

// RM with two clock domains — tests multi-clock partition support
module dual_clock_rm (
    input wire fast_clk,
    input wire slow_clk,

    input wire [31:0]  data_fast,
    output reg [31:0]  result_slow
);

    reg [31:0] fast_latch;

    always @(posedge fast_clk) begin
        fast_latch <= data_fast;
    end

    always @(posedge slow_clk) begin
        result_slow <= fast_latch;
    end

endmodule
