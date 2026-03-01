`timescale 1ns/1ps

// RM with mixed port widths: 1-bit, 8-bit, 16-bit, 32-bit
module mixed_width_rm (
    input wire        clk,

    // Inputs from static (to_rm)
    input wire        enable,      // 1-bit
    input wire [7:0]  byte_in,     // 8-bit
    input wire [15:0] half_in,     // 16-bit
    input wire [31:0] word_in,     // 32-bit

    // Outputs to static (from_rm)
    output reg        flag_out,    // 1-bit
    output reg [7:0]  status_byte, // 8-bit
    output reg [15:0] result_half, // 16-bit
    output reg [31:0] result_word  // 32-bit
);

    always @(posedge clk) begin
        if (enable) begin
            flag_out    <= |byte_in;           // OR-reduce: any bit set?
            status_byte <= byte_in + 8'd1;     // byte + 1
            result_half <= half_in + 16'd100;  // half + 100
            result_word <= word_in * 2;        // word * 2
        end else begin
            flag_out    <= 1'b0;
            status_byte <= 8'd0;
            result_half <= 16'd0;
            result_word <= 32'd0;
        end
    end

endmodule
