`timescale 1ns/1ps

// Static region where the clock is named something pyslang won't auto-detect
module weird_clock_static (
    input wire sys_input,  // This is actually the clock, but no clk/clock in name

    output reg [31:0] counter
);

    always @(posedge sys_input) begin
        counter <= counter + 1;
    end

    // Partition RM instantiation (bridge replaces this)
    weird_rm u_weird_rm (
        .sys_input(sys_input),
        .data_out(/* unused */)
    );

endmodule
