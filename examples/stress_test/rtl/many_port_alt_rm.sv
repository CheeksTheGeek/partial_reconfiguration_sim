`timescale 1ns/1ps

// Alternate RM for rp_many: multiply instead of add
module many_port_alt_rm (
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

    // Different behavior: running accumulator
    reg [31:0] acc;
    always @(posedge clk) begin
        acc   <= acc + 1;
        out_0 <= in_0 - in_1;
        out_1 <= in_2 - in_3;
        out_2 <= acc;
        out_3 <= in_0 + in_1 + in_2 + in_3;
    end

endmodule
