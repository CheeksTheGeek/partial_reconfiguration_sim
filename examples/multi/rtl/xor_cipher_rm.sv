`timescale 1ns/1ps

module xor_cipher_rm (
    input wire clk,

    input wire [31:0] operand_a,
    input wire [31:0] operand_b,

    output wire [31:0] result     // ciphertext
);
    assign result = operand_a ^ operand_b;
endmodule
