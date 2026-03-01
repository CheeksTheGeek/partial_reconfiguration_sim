`timescale 1ns/1ps

module static_region (
    input wire clk,

    // --- Readable static outputs ---
    output reg [31:0] tick_counter,

    // --- Partition rp_wide: tests mixed-width ports ---
    // (replaced by DPI bridge at build time)
    output wire       flag_out,        // 1-bit from_rm
    output wire [7:0] status_byte_out, // 8-bit from_rm
    output wire [15:0] result_half_out, // 16-bit from_rm
    output wire [31:0] result_word_out, // 32-bit from_rm

    // --- Partition rp_many: tests many-port boundary ---
    output wire [31:0] many_out_0,
    output wire [31:0] many_out_1,
    output wire [31:0] many_out_2,
    output wire [31:0] many_out_3
);

    // Static tick counter
    always @(posedge clk) begin
        tick_counter <= tick_counter + 1;
    end

    // ----- Partition rp_wide -----
    reg        enable;
    reg  [7:0] byte_val;
    reg [15:0] half_val;
    reg [31:0] word_val;

    always @(posedge clk) begin
        enable   <= tick_counter[0];
        byte_val <= tick_counter[7:0];
        half_val <= tick_counter[15:0];
        word_val <= tick_counter;
    end

    // Instantiation replaced by bridge
    mixed_width_rm u_mixed_width_rm (
        .clk(clk),
        .enable(enable),
        .byte_in(byte_val),
        .half_in(half_val),
        .word_in(word_val),
        .flag_out(flag_out),
        .status_byte(status_byte_out),
        .result_half(result_half_out),
        .result_word(result_word_out)
    );

    // ----- Partition rp_many -----
    reg [31:0] src_0, src_1, src_2, src_3;
    always @(posedge clk) begin
        src_0 <= tick_counter;
        src_1 <= tick_counter + 1;
        src_2 <= tick_counter + 2;
        src_3 <= tick_counter + 3;
    end

    many_port_rm u_many_port_rm (
        .clk(clk),
        .in_0(src_0), .in_1(src_1), .in_2(src_2), .in_3(src_3),
        .out_0(many_out_0), .out_1(many_out_1),
        .out_2(many_out_2), .out_3(many_out_3)
    );

endmodule
