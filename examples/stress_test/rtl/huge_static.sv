`timescale 1ns/1ps
module huge_static (
    input wire clk,
    output reg [31:0] tick
);
    always @(posedge clk) tick <= tick + 1;

    wide128_rm u_wide128_rm (
        .clk(clk),
        .huge_in(128'd0),
        .huge_out()
    );
endmodule
