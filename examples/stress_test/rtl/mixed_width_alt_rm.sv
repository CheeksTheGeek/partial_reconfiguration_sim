`timescale 1ns/1ps

// Alternate RM for rp_wide: different behavior, same interface
module mixed_width_alt_rm (
    input wire        clk,

    input wire        enable,
    input wire [7:0]  byte_in,
    input wire [15:0] half_in,
    input wire [31:0] word_in,

    output reg        flag_out,
    output reg [7:0]  status_byte,
    output reg [15:0] result_half,
    output reg [31:0] result_word
);

    // Different behavior: invert everything
    always @(posedge clk) begin
        flag_out    <= ~enable;
        status_byte <= ~byte_in;
        result_half <= ~half_in;
        result_word <= ~word_in;
    end

endmodule
