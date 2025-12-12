`timescale 1ns/1ps

module adder_rm (
    input wire clk,

    // Operands - writable via generated API
    input wire [31:0] operand_a,
    input wire [31:0] operand_b,

    // Result - readable via generated API
    output wire [31:0] result
);

    // Simple combinational adder
    assign result = operand_a + operand_b;

endmodule
