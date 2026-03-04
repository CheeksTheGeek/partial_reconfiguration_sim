`timescale 1ns/1ps

// RM with active-low reset — tests reset protocol enforcement
// On reset: state <= 0xDEAD_BEEF (distinctive value)
// Without reset: Verilator defaults registers to 0
module resettable_rm (
    input wire clk,
    input wire rst_n,

    output reg [31:0] state
);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            state <= 32'hDEAD_BEEF;
        else
            state <= state + 1;
    end

endmodule
