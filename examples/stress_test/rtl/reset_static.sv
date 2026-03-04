`timescale 1ns/1ps
module reset_static (
    input wire clk,
    output reg [31:0] tick
);
    always @(posedge clk) tick <= tick + 1;

    // Instantiation replaced by DPI bridge at build time
    resettable_rm u_resettable_rm (
        .clk(clk),
        .rst_n(1'b1),
        .state()
    );
endmodule
