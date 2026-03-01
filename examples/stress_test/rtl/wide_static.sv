`timescale 1ns/1ps
module wide_static (
    input wire clk,
    output reg [31:0] tick
);
    always @(posedge clk) tick <= tick + 1;

    wide64_rm u_wide64_rm (
        .clk(clk),
        .wide_in(48'd0),
        .wide_out(),
        .full64_in(64'd0),
        .full64_out()
    );
endmodule
