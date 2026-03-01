`timescale 1ns/1ps

// RM with a 48-bit and a 64-bit port — tests >32-bit DPI path
module wide64_rm (
    input wire clk,

    input wire [47:0]  wide_in,     // 48-bit input
    output reg [47:0]  wide_out,    // 48-bit output
    input wire [63:0]  full64_in,   // 64-bit input
    output reg [63:0]  full64_out   // 64-bit output
);

    always @(posedge clk) begin
        wide_out   <= wide_in + 48'd1;
        full64_out <= full64_in + 64'd1;
    end

endmodule
