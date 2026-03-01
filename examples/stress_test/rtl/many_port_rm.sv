`timescale 1ns/1ps

// RM with 4 inputs and 4 outputs (8 boundary ports total)
module many_port_rm (
    input wire clk,

    input wire [31:0] in_0,
    input wire [31:0] in_1,
    input wire [31:0] in_2,
    input wire [31:0] in_3,

    output reg [31:0] out_0,
    output reg [31:0] out_1,
    output reg [31:0] out_2,
    output reg [31:0] out_3
);

    // Sum adjacent pairs
    always @(posedge clk) begin
        out_0 <= in_0 + in_1;
        out_1 <= in_2 + in_3;
        out_2 <= in_0 ^ in_2;  // XOR
        out_3 <= in_1 ^ in_3;  // XOR
    end

endmodule
