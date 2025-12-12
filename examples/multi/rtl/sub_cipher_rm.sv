`timescale 1ns/1ps

module sub_cipher_rm (
    input wire clk,

    input wire [31:0] operand_a,
    input wire [31:0] operand_b,
    output wire [31:0] result
);
    function [7:0] substitute_byte;
        input [7:0] b;
        reg [7:0] rotated;
        begin
            rotated = (b << 3) | (b >> 5);
            substitute_byte = ~rotated;
        end
    endfunction

    assign result = {
        substitute_byte(operand_a[31:24]),
        substitute_byte(operand_a[23:16]),
        substitute_byte(operand_a[15:8]),
        substitute_byte(operand_a[7:0])
    };

endmodule
