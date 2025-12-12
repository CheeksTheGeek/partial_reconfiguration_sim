`timescale 1ns/1ps

module subtractor_rm (
    input wire clk,
    input wire [31:0] operand_a,
    input wire [31:0] operand_b,
    output wire [31:0] result
);
    assign result = operand_a - operand_b;

endmodule
