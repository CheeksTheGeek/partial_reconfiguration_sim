`timescale 1ns/1ps

// RM with a 128-bit port — exceeds DPI longint (64-bit) limit
module wide128_rm (
    input wire clk,

    input wire [127:0]  huge_in,
    output reg [127:0]  huge_out
);

    always @(posedge clk) begin
        huge_out <= huge_in + 128'd1;
    end

endmodule
