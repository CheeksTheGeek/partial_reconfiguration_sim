`timescale 1ns/1ps
module dual_clock_static (
    input wire clk,
    output reg [31:0] tick
);
    // Both partition clocks derived from static clock
    wire fast_clk = clk;
    wire slow_clk = clk;

    always @(posedge clk) tick <= tick + 1;

    // Instantiation replaced by DPI bridge at build time
    dual_clock_rm u_dual_clock_rm (
        .fast_clk(fast_clk),
        .slow_clk(slow_clk),
        .data_fast(32'd0),
        .result_slow()
    );
endmodule
