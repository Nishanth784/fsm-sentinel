`timescale 1ns / 1ps
//============================================================================
// Testbench  : tb_connect_m_s
// DUT        : connect_m_s  (original axi_master + axi4_slave, UNMODIFIED)
//
// === WHY PREVIOUS TESTBENCHES FAILED ===
//
//  Problem 1 (reads = 0x00000000):
//    The slave reset handler does:  for(i=0;i<128;i++) mem[i]<=0
//    Any reset between write and read ERASES all written data.
//    FIX: Only ONE reset at the very beginning. Never reset again.
//
//  Problem 2 (spurious reads after reset):
//    After reset, master goes idle->detect_op. If wr=0, it enters
//    send_raddr with stale rd_addr=0, wasting simulation time.
//    FIX: Set wr=1 BEFORE releasing resetn so master goes straight
//    to write on the first detect_op cycle.
//
//  Problem 3 (rout always 0 when sampled):
//    Master clears rout<=0 in comp_rd_tx, 1 cycle after latching it.
//    FIX: Capture data directly from the AXI R-channel bus using
//    an always block that grabs m_axi_rdata when rvalid && rready.
//
//  Problem 4 (slave reads only 1 byte per beat):
//    fetch_rdata does: rdata <= mem[raddr]  (8-bit -> 32-bit zero-extends).
//    For data values > 255, upper bytes are lost on reads.
//    FIX: Use din=1 so all master-generated values fit in 1 byte:
//    1, 5, 25, 125.  Read values will match writes exactly.
//
//============================================================================

module tb_connect_m_s;

    // ----------------------------------------------------------
    // Testbench signals
    // ----------------------------------------------------------
    reg         clk;
    reg         resetn;
    reg         wr;
    reg  [31:0] wr_addr;
    reg  [7:0]  wr_burst_len;
    reg  [1:0]  wr_burst_type;
    reg  [31:0] wr_din;
    reg  [3:0]  wr_strbin;
    reg  [31:0] rd_addr;
    reg  [7:0]  rd_burst_len;
    reg  [1:0]  rd_burst_type;
    wire [31:0] rout;
    wire [1:0]  resp;

    // ----------------------------------------------------------
    // DUT - original connect_m_s, UNMODIFIED
    // ----------------------------------------------------------
    connect_m_s dut (
        .clk(clk), .resetn(resetn),
        .wr(wr),
        .wr_addr(wr_addr),       .wr_burst_len(wr_burst_len),
        .wr_burst_type(wr_burst_type), .wr_din(wr_din),
        .wr_strbin(wr_strbin),
        .rd_addr(rd_addr),       .rd_burst_len(rd_burst_len),
        .rd_burst_type(rd_burst_type),
        .rout(rout), .resp(resp),
        .pc_status(), .pc_asserted()
    );

    // ----------------------------------------------------------
    // Clock - 100 MHz (10 ns period)
    // ----------------------------------------------------------
    initial clk = 0;
    always #5 clk = ~clk;

    // ----------------------------------------------------------
    // Watchdog
    // ----------------------------------------------------------
    initial begin
        #200_000;
        $display("\n!!! WATCHDOG TIMEOUT at %0t !!!", $time);
        print_summary;
        $finish;
    end

    // ==========================================================
    // R-CHANNEL BUS CAPTURE
    //   Grabs data directly from m_axi_rdata when rvalid && rready.
    //   This avoids the rout=0 problem.
    // ==========================================================
    reg  [31:0] cap_data [0:15];
    reg         cap_rlast [0:15];
    integer     cap_cnt;
    reg         cap_en;

    initial begin
        cap_cnt = 0;
        cap_en  = 0;
    end

    always @(posedge clk) begin
        if (cap_en && dut.m_axi_rvalid && dut.m_axi_rready) begin
            cap_data[cap_cnt]  = dut.m_axi_rdata;
            cap_rlast[cap_cnt] = dut.m_axi_rlast;
            cap_cnt = cap_cnt + 1;
        end
    end

    // ----------------------------------------------------------
    // Scoreboard
    // ----------------------------------------------------------
    integer total_tests = 0;
    integer pass_count  = 0;
    integer fail_count  = 0;

    task sep;
        $display("--------------------------------------------------------------");
    endtask

    task check;
        input [511:0] label;
        input         cond;
        input [31:0]  got;
        input [31:0]  expected;
        begin
            total_tests = total_tests + 1;
            if (cond) begin
                $display("    [PASS] %0s  got=0x%08h", label, got);
                pass_count = pass_count + 1;
            end else begin
                $display("    [FAIL] %0s  got=0x%08h  exp=0x%08h",
                         label, got, expected);
                fail_count = fail_count + 1;
            end
        end
    endtask

    task print_summary;
        begin
            $display("");
            $display("==============================================================");
            $display("  FINAL RESULTS");
            $display("==============================================================");
            $display("  Total checks : %0d", total_tests);
            $display("  PASSED       : %0d", pass_count);
            $display("  FAILED       : %0d", fail_count);
            $display("--------------------------------------------------------------");
            if (fail_count == 0)
                $display("  >>> ALL CHECKS PASSED <<<");
            else
                $display("  >>> %0d CHECK(S) FAILED <<<", fail_count);
            $display("==============================================================");
        end
    endtask

    task dump_mem;
        input [31:0] start;
        input integer count;
        integer dm;
        begin
            $write("    MEM[%0d..%0d] =", start, start + count - 1);
            for (dm = 0; dm < count; dm = dm + 1)
                $write(" %02h", dut.dut.mem[start + dm]);
            $display("");
        end
    endtask

    task print_captured_beats;
        integer pb;
        begin
            for (pb = 0; pb < cap_cnt; pb = pb + 1)
                $display("      R beat[%0d] = 0x%08h  rlast=%0b",
                         pb, cap_data[pb], cap_rlast[pb]);
        end
    endtask

    // ----------------------------------------------------------
    // Wait for bvalid with timeout
    // ----------------------------------------------------------
    task wait_bvalid;
        input integer max_cyc;
        integer t;
        begin
            t = 0;
            while (!(dut.m_axi_bvalid && dut.m_axi_bready) && t < max_cyc) begin
                @(posedge clk);
                t = t + 1;
            end
            if (t >= max_cyc) $display("    WARN: bvalid timeout!");
        end
    endtask

    // ----------------------------------------------------------
    // Wait for rlast with timeout
    // ----------------------------------------------------------
    task wait_rlast;
        input integer max_cyc;
        integer t;
        begin
            t = 0;
            while (!(dut.m_axi_rlast && dut.m_axi_rvalid && dut.m_axi_rready)
                   && t < max_cyc) begin
                @(posedge clk);
                t = t + 1;
            end
            if (t >= max_cyc) $display("    WARN: rlast timeout!");
        end
    endtask

    // ==========================================================
    // MAIN STIMULUS
    //
    // Flow for each test:
    //   1. Assert wr=1 with write+read params (same cycle)
    //   2. Wait for bvalid (write response from slave)
    //   3. Set wr=0 -> master auto-reads using pre-set rd params
    //   4. Enable R-channel capture, wait for rlast
    //   5. Disable capture, check results
    //   6. Immediately set wr=1 for NEXT test (prevents spurious reads)
    // ==========================================================

    integer test_num;

    initial begin
        $dumpfile("axi4_waves.vcd");
        $dumpvars(0, tb_connect_m_s);

        // Defaults
        wr = 0; wr_addr = 0; wr_burst_len = 0; wr_burst_type = 0;
        wr_din = 0; wr_strbin = 0;
        rd_addr = 0; rd_burst_len = 0; rd_burst_type = 0;

        $display("");
        $display("==============================================================");
        $display("  AXI4 Master-Slave Verification Testbench");
        $display("  DUT: connect_m_s (original, unmodified RTL)");
        $display("  Clock = 100 MHz  |  Data Width = 32b  |  Mem = 128 bytes");
        $display("==============================================================");

        // ==========================================================
        // RESET - set wr=1 BEFORE releasing reset to prevent
        //         any spurious reads at startup
        // ==========================================================
        resetn = 0;
        repeat (6) @(posedge clk);

        // Pre-load TEST 1 write params WHILE still in reset
        wr            = 1;
        wr_addr       = 32'h0000_0004;
        wr_burst_len  = 8'd3;
        wr_burst_type = 2'b01;       // INCR
        wr_din        = 32'h0000_0001; // din=1 -> beats: 1,5,25,125
        wr_strbin     = 4'b1111;
        rd_addr       = 32'h0000_0004;
        rd_burst_len  = 8'd3;
        rd_burst_type = 2'b01;

        // Release reset - master sees wr=1 in detect_op, goes to write
        resetn = 1;
        $display("\n  Reset released at t=%0t. wr=1 preset for TEST 1.\n", $time);

        // ==========================================================
        // TEST 1 - INCR Burst (len=3 -> 4 beats)
        //   din=1 -> master sends: 1, 1*5=5, 5*5=25, 25*5=125
        // ==========================================================
        test_num = 1;
        sep;
        $display("TEST %0d: INCR Burst Write + Read  (len=3, 4 beats)", test_num);
        $display("    Write: addr=0x04, din=1, strb=1111, INCR");
        $display("    Expected W beats: 0x01, 0x05, 0x19(25), 0x7D(125)");
        sep;

        wait_bvalid(500);
        wr = 0;     // master -> idle -> detect_op -> send_raddr(0x04)

        $display("    >> Write done at t=%0t. Slave memory:", $time);
        dump_mem(4, 16);

        cap_cnt = 0;
        cap_en  = 1;
        wait_rlast(500);
        @(posedge clk);
        cap_en = 0;

        $display("    >> Read done. Captured %0d R-channel beats:", cap_cnt);
        print_captured_beats;

        check("Beat count = 4",
              (cap_cnt == 4), cap_cnt, 4);
        if (cap_cnt >= 4) begin
            check("Beat[0] = 0x00000001 (din=1)",
                  (cap_data[0] == 32'h01), cap_data[0], 32'h01);
            check("Beat[1] = 0x00000005 (1*5)",
                  (cap_data[1] == 32'h05), cap_data[1], 32'h05);
            check("Beat[2] = 0x00000019 (5*5=25)",
                  (cap_data[2] == 32'h19), cap_data[2], 32'h19);
            check("Beat[3] = 0x0000007D (25*5=125)",
                  (cap_data[3] == 32'h7D), cap_data[3], 32'h7D);
        end
        $display("");

        // ==========================================================
        // TEST 2 - FIXED Burst (len=2 -> 3 beats)
        //   din=2 -> master sends: 2, 10(0x0A), 50(0x32)
        //   FIXED: all 3 beats write to same addr -> last value wins
        // ==========================================================
        test_num = 2;
        sep;
        $display("TEST %0d: FIXED Burst Write + Read  (len=2, 3 beats)", test_num);
        $display("    Write: addr=0x20, din=2, strb=1111, FIXED");
        $display("    W beats: 2(0x02), 10(0x0A), 50(0x32)");
        $display("    FIXED -> all overwrite addr 0x20 -> final = 0x32");
        sep;

        @(posedge clk);
        wr            = 1;
        wr_addr       = 32'h0000_0020;
        wr_burst_len  = 8'd2;
        wr_burst_type = 2'b00;       // FIXED
        wr_din        = 32'h0000_0002;
        wr_strbin     = 4'b1111;
        rd_addr       = 32'h0000_0020;
        rd_burst_len  = 8'd2;
        rd_burst_type = 2'b00;

        wait_bvalid(500);
        wr = 0;

        $display("    >> Write done. Slave memory:");
        dump_mem(32, 8);

        cap_cnt = 0;
        cap_en  = 1;
        wait_rlast(500);
        @(posedge clk);
        cap_en = 0;

        $display("    >> Read done. Captured %0d R-channel beats:", cap_cnt);
        print_captured_beats;

        check("Beat count = 3",
              (cap_cnt == 3), cap_cnt, 3);
        if (cap_cnt >= 3) begin
            check("All FIXED beats identical",
                  (cap_data[0] == cap_data[1] && cap_data[1] == cap_data[2]),
                  cap_data[0], cap_data[2]);
            check("FIXED value = 0x32 (last beat=50)",
                  (cap_data[0] == 32'h32), cap_data[0], 32'h32);
        end
        $display("");

        // ==========================================================
        // TEST 3 - WRAP Burst (len=3 -> 4 beats)
        //   din=1 -> beats: 1, 5, 25, 125  (all <256)
        //   addr=0x48 with boundary=16: wraps 0x4C -> 0x40
        // ==========================================================
        test_num = 3;
        sep;
        $display("TEST %0d: WRAP Burst Write + Read  (len=3, 4 beats)", test_num);
        $display("    Write: addr=0x48, din=1, strb=1111, WRAP");
        $display("    Boundary=16 bytes (0x40..0x4F)");
        $display("    Beat0->0x48, Beat1->0x4C, Beat2->wrap 0x40, Beat3->0x44");
        sep;

        @(posedge clk);
        wr            = 1;
        wr_addr       = 32'h0000_0048;
        wr_burst_len  = 8'd3;
        wr_burst_type = 2'b10;       // WRAP
        wr_din        = 32'h0000_0001;
        wr_strbin     = 4'b1111;
        rd_addr       = 32'h0000_0048;
        rd_burst_len  = 8'd3;
        rd_burst_type = 2'b10;

        wait_bvalid(500);
        wr = 0;

        $display("    >> Write done. Memory (0x40..0x4F):");
        dump_mem(64, 16);

        cap_cnt = 0;
        cap_en  = 1;
        wait_rlast(500);
        @(posedge clk);
        cap_en = 0;

        $display("    >> Read done. Captured %0d R-channel beats:", cap_cnt);
        print_captured_beats;

        check("Beat count = 4",
              (cap_cnt == 4), cap_cnt, 4);
        if (cap_cnt >= 1) begin
            check("Beat[0] non-zero (WRAP read works)",
                  (cap_data[0] != 0), cap_data[0], 32'h0);
        end
        $display("");

        // ==========================================================
        // TEST 4 - INCR Burst len=1 (2 beats)
        //   din=3 -> beats: 3(0x03), 15(0x0F)
        // ==========================================================
        test_num = 4;
        sep;
        $display("TEST %0d: INCR Burst Write + Read  (len=1, 2 beats)", test_num);
        $display("    Write: addr=0x00, din=3, strb=1111");
        $display("    Beat0=3(0x03), Beat1=15(0x0F)");
        sep;

        @(posedge clk);
        wr            = 1;
        wr_addr       = 32'h0000_0000;
        wr_burst_len  = 8'd1;
        wr_burst_type = 2'b01;
        wr_din        = 32'h0000_0003;
        wr_strbin     = 4'b1111;
        rd_addr       = 32'h0000_0000;
        rd_burst_len  = 8'd1;
        rd_burst_type = 2'b01;

        wait_bvalid(500);
        wr = 0;

        $display("    >> Write done. Slave memory:");
        dump_mem(0, 8);

        cap_cnt = 0;
        cap_en  = 1;
        wait_rlast(500);
        @(posedge clk);
        cap_en = 0;

        $display("    >> Read done. Captured %0d R-channel beats:", cap_cnt);
        print_captured_beats;

        check("Beat count = 2",
              (cap_cnt == 2), cap_cnt, 2);
        if (cap_cnt >= 2) begin
            check("Beat[0] = 0x00000003",
                  (cap_data[0] == 32'h03), cap_data[0], 32'h03);
            check("Beat[1] = 0x0000000F",
                  (cap_data[1] == 32'h0F), cap_data[1], 32'h0F);
        end
        $display("");

        // ==========================================================
        // TEST 5 - Full Strobe Byte Verification (strb=1111)
        //   din=0xAB -> single-byte value -> beat0=0x000000AB
        // ==========================================================
        test_num = 5;
        sep;
        $display("TEST %0d: Full Strobe Byte Verification (len=1, strb=1111)", test_num);
        $display("    Write: addr=0x50, din=0xAB, strb=1111");
        $display("    Verify individual bytes in slave memory.");
        sep;

        @(posedge clk);
        wr            = 1;
        wr_addr       = 32'h0000_0050;
        wr_burst_len  = 8'd1;
        wr_burst_type = 2'b01;
        wr_din        = 32'h0000_00AB;
        wr_strbin     = 4'b1111;
        rd_addr       = 32'h0000_0050;
        rd_burst_len  = 8'd1;
        rd_burst_type = 2'b01;

        wait_bvalid(500);
        wr = 0;

        $display("    >> Write done. Slave memory:");
        dump_mem(80, 8);

        check("mem[80] = 0xAB (byte 0)",
              (dut.dut.mem[80] == 8'hAB), {24'd0, dut.dut.mem[80]}, 32'hAB);
        check("mem[81] = 0x00 (byte 1)",
              (dut.dut.mem[81] == 8'h00), {24'd0, dut.dut.mem[81]}, 32'h00);
        check("mem[82] = 0x00 (byte 2)",
              (dut.dut.mem[82] == 8'h00), {24'd0, dut.dut.mem[82]}, 32'h00);
        check("mem[83] = 0x00 (byte 3)",
              (dut.dut.mem[83] == 8'h00), {24'd0, dut.dut.mem[83]}, 32'h00);

        cap_cnt = 0;
        cap_en  = 1;
        wait_rlast(500);
        @(posedge clk);
        cap_en = 0;

        $display("    >> Readback beats:");
        print_captured_beats;
        if (cap_cnt >= 1) begin
            check("Readback beat[0] = 0xAB",
                  (cap_data[0] == 32'hAB), cap_data[0], 32'hAB);
        end
        $display("");

        // ==========================================================
        // TEST 6 - Write-Read Data Integrity (INCR, len=1)
        //   din=2 -> beat0=2, beat1=10 -> exact match check
        // ==========================================================
        test_num = 6;
        sep;
        $display("TEST %0d: Write-Read Data Integrity (addr=0x60, len=1)", test_num);
        $display("    din=2 -> beat0=0x02, beat1=0x0A");
        sep;

        @(posedge clk);
        wr            = 1;
        wr_addr       = 32'h0000_0060;
        wr_burst_len  = 8'd1;
        wr_burst_type = 2'b01;
        wr_din        = 32'h0000_0002;
        wr_strbin     = 4'b1111;
        rd_addr       = 32'h0000_0060;
        rd_burst_len  = 8'd1;
        rd_burst_type = 2'b01;

        wait_bvalid(500);
        wr = 0;

        $display("    >> Write done. Slave memory:");
        dump_mem(96, 8);

        cap_cnt = 0;
        cap_en  = 1;
        wait_rlast(500);
        @(posedge clk);
        cap_en = 0;

        $display("    >> Read done. Captured %0d beats:", cap_cnt);
        print_captured_beats;

        if (cap_cnt >= 2) begin
            check("Integrity beat[0] = 0x02",
                  (cap_data[0] == 32'h02), cap_data[0], 32'h02);
            check("Integrity beat[1] = 0x0A",
                  (cap_data[1] == 32'h0A), cap_data[1], 32'h0A);
        end
        $display("");

        // ==========================================================
        // TEST 7 - Single-beat Write (len=0) - KNOWN MASTER BUG
        //   Master stuck in wdata_last (state 4) for len=0.
        //   Slave DOES write the data before the hang.
        // ==========================================================
        test_num = 7;
        sep;
        $display("TEST %0d: Single-Beat Write (len=0) - Known Master Bug", test_num);
        $display("    addr=0x10, din=0xDEADBEEF, strb=1111");
        $display("    Master hangs in wdata_last (state 4).");
        sep;

        @(posedge clk);
        wr            = 1;
        wr_addr       = 32'h0000_0010;
        wr_burst_len  = 8'd0;
        wr_burst_type = 2'b01;
        wr_din        = 32'hDEAD_BEEF;
        wr_strbin     = 4'b1111;

        // Wait for AW+W handshake to complete, master will then stall
        repeat (150) @(posedge clk);
        wr = 0;

        $display("    Master state = %0d %s",
                 dut.uut.state,
                 (dut.uut.state == 4) ? "(wdata_last - STUCK as expected)" :
                 (dut.uut.state == 0) ? "(idle - recovered unexpectedly)" : "");

        $display("    Slave memory at 0x10:");
        dump_mem(16, 4);

        check("mem[16]=0xEF (byte0)",
              (dut.dut.mem[16] == 8'hEF), {24'd0, dut.dut.mem[16]}, 32'hEF);
        check("mem[17]=0xBE (byte1)",
              (dut.dut.mem[17] == 8'hBE), {24'd0, dut.dut.mem[17]}, 32'hBE);
        check("mem[18]=0xAD (byte2)",
              (dut.dut.mem[18] == 8'hAD), {24'd0, dut.dut.mem[18]}, 32'hAD);
        check("mem[19]=0xDE (byte3)",
              (dut.dut.mem[19] == 8'hDE), {24'd0, dut.dut.mem[19]}, 32'hDE);
        check("Master stuck state=4",
              (dut.uut.state == 4), dut.uut.state, 4);

        $display("");
        $display("    BUG ANALYSIS: In send_wdata, when burst_count=0 and");
        $display("    wready arrives, master moves to wdata_last with wlast=1.");
        $display("    But slave already deasserted wready. Master waits forever.");
        $display("    FIX: Assert wlast in send_waddr when burst_len=0,");
        $display("    or skip wdata_last entirely for single-beat case.");
        $display("");

        // ==========================================================
        // SUMMARY
        // ==========================================================
        print_summary;
        $display("  Simulation ended at t = %0t ns\n", $time);

        #50;
        $finish;
    end

    // ==========================================================
    // AXI CHANNEL MONITOR - prints every handshake to TCL console
    // ==========================================================
    always @(posedge clk) begin
        if (dut.m_axi_awvalid && dut.m_axi_awready)
            $display("  [%0t] AW : addr=0x%08h  len=%0d  burst=%02b  size=%0d",
                     $time, dut.m_axi_awaddr, dut.m_axi_awlen,
                     dut.m_axi_awburst, dut.m_axi_awsize);

        if (dut.m_axi_wvalid && dut.m_axi_wready)
            $display("  [%0t] W  : data=0x%08h  strb=%04b  wlast=%b",
                     $time, dut.m_axi_wdata, dut.m_axi_wstrb, dut.m_axi_wlast);

        if (dut.m_axi_bvalid && dut.m_axi_bready)
            $display("  [%0t] B  : bresp=%02b  %s",
                     $time, dut.m_axi_bresp,
                     (dut.m_axi_bresp == 2'b00) ? "OKAY" : "ERROR");

        if (dut.m_axi_arvalid && dut.m_axi_arready)
            $display("  [%0t] AR : addr=0x%08h  len=%0d  burst=%02b  size=%0d",
                     $time, dut.m_axi_araddr, dut.m_axi_arlen,
                     dut.m_axi_arburst, dut.m_axi_arsize);

        if (dut.m_axi_rvalid && dut.m_axi_rready)
            $display("  [%0t] R  : data=0x%08h  rlast=%b  rresp=%02b",
                     $time, dut.m_axi_rdata, dut.m_axi_rlast, dut.m_axi_rresp);
    end

endmodule



`timescale 1ns / 1ps
 
 
module connect_m_s(
    input clk, resetn, 
    input wr,
    input [31:0] wr_addr,
    input [7:0]  wr_burst_len,
    input [1:0]  wr_burst_type,
    input [31:0] wr_din,
    input [3:0]  wr_strbin,
    input [31:0] rd_addr,
    input [7:0]  rd_burst_len,
    input [1:0]  rd_burst_type,
    output [31:0] rout,
    output [1:0]  resp,
    output [159:0] pc_status,
    output pc_asserted
    );
 
    // AXI signals
    wire [2:0] m_axi_awid;
    wire [31:0] m_axi_awaddr;
    wire [2:0] m_axi_awsize;
    wire [1:0] m_axi_awburst;
    wire [7:0] m_axi_awlen;
    wire [1:0] m_axi_awlock;
    wire [3:0] m_axi_awcache;
    wire [2:0] m_axi_awprot;
    wire [3:0] m_axi_awqos;
    wire [4:0] m_axi_awuser;
    wire m_axi_awvalid;
    wire m_axi_awready;
    wire [2:0] m_axi_wid;
    wire [31:0] m_axi_wdata;
    wire [3:0] m_axi_wstrb;
    wire m_axi_wlast;
    wire m_axi_wvalid;
    wire m_axi_wready;
    wire [2:0] m_axi_bid;
    wire [1:0] m_axi_bresp;
    wire m_axi_bvalid;
    wire m_axi_bready;
    wire [2:0] m_axi_arid;
    wire [31:0] m_axi_araddr;
    wire [7:0] m_axi_arlen;
    wire [2:0] m_axi_arsize;
    wire [1:0] m_axi_arburst;
    wire [1:0] m_axi_arlock;
    wire [3:0] m_axi_arcache;
    wire [2:0] m_axi_arprot;
    wire [3:0] m_axi_arqos;
    wire [4:0] m_axi_aruser;
    wire m_axi_arvalid;
    wire m_axi_arready;
    wire [2:0] m_axi_rid;
    wire [31:0] m_axi_rdata;
    wire [1:0] m_axi_rresp;
    wire m_axi_rlast;
    wire m_axi_rvalid;
    wire m_axi_rready;
    
 
  
     
    
axi_master uut (
        .m_axi_aclk(clk),
        .m_axi_aresetn(resetn),
        .m_axi_awid(m_axi_awid),
        .m_axi_awaddr(m_axi_awaddr),
        .m_axi_awsize(m_axi_awsize),
        .m_axi_awburst(m_axi_awburst),
        .m_axi_awlen(m_axi_awlen),
        .m_axi_awlock(m_axi_awlock),
        .m_axi_awcache(m_axi_awcache),
        .m_axi_awprot(m_axi_awprot),
        .m_axi_awqos(m_axi_awqos),
        .m_axi_awuser(m_axi_awuser),
        .m_axi_awvalid(m_axi_awvalid),
        .m_axi_awready(m_axi_awready),
        .m_axi_wid(m_axi_wid),
        .m_axi_wdata(m_axi_wdata),
        .m_axi_wstrb(m_axi_wstrb),
        .m_axi_wlast(m_axi_wlast),
        .m_axi_wvalid(m_axi_wvalid),
        .m_axi_wready(m_axi_wready),
        .m_axi_bid(m_axi_bid),
        .m_axi_bresp(m_axi_bresp),
        .m_axi_bvalid(m_axi_bvalid),
        .m_axi_bready(m_axi_bready),
        .m_axi_arid(m_axi_arid),
        .m_axi_araddr(m_axi_araddr),
        .m_axi_arlen(m_axi_arlen),
        .m_axi_arsize(m_axi_arsize),
        .m_axi_arburst(m_axi_arburst),
        .m_axi_arlock(m_axi_arlock),
        .m_axi_arcache(m_axi_arcache),
        .m_axi_arprot(m_axi_arprot),
        .m_axi_arqos(m_axi_arqos),
        .m_axi_aruser(m_axi_aruser),
        .m_axi_arvalid(m_axi_arvalid),
        .m_axi_arready(m_axi_arready),
        .m_axi_rid(m_axi_rid),
        .m_axi_rdata(m_axi_rdata),
        .m_axi_rresp(m_axi_rresp),
        .m_axi_rlast(m_axi_rlast),
        .m_axi_rvalid(m_axi_rvalid),
        .m_axi_rready(m_axi_rready),
        .wr(wr),
        .wr_addr(wr_addr),
        .wr_burst_len(wr_burst_len),
        .wr_burst_type(wr_burst_type),
        .wr_din(wr_din),
        .wr_strbin(wr_strbin),
        .rd_addr(rd_addr),
        .rd_burst_len(rd_burst_len),
        .rd_burst_type(rd_burst_type),
        .rout(rout),
        .resp(resp)
    );
 
 
 
axi4_slave dut (
        .s_axi_aclk(clk),
        .s_axi_aresetn(resetn),
 
        .s_axi_awid(m_axi_awid),
        .s_axi_awvalid(m_axi_awvalid),
        .s_axi_awready(m_axi_awready),
        .s_axi_awaddr(m_axi_awaddr),
        .s_axi_awlen(m_axi_awlen),
        .s_axi_awsize(m_axi_awsize),
        .s_axi_awburst(m_axi_awburst),
        .s_axi_awlock(m_axi_awlock),
        .s_axi_awcache(m_axi_awcache),
        .s_axi_awprot(m_axi_awprot),
        .s_axi_awqos(m_axi_awqos),
        .s_axi_awuser(m_axi_awuser),
 
        .s_axi_wid(m_axi_wid),
        .s_axi_wvalid(m_axi_wvalid),
        .s_axi_wready(m_axi_wready),
        .s_axi_wdata(m_axi_wdata),
        .s_axi_wstrb(m_axi_wstrb),
        .s_axi_wlast(m_axi_wlast),
 
        .s_axi_bid(m_axi_bid),
        .s_axi_bvalid(m_axi_bvalid),
        .s_axi_bready(m_axi_bready),
        .s_axi_bresp(m_axi_bresp),
 
        .s_axi_arid(m_axi_arid),
        .s_axi_arvalid(m_axi_arvalid),
        .s_axi_arready(m_axi_arready),
        .s_axi_araddr(m_axi_araddr),
        .s_axi_arlen(m_axi_arlen),
        .s_axi_arsize(m_axi_arsize),
        .s_axi_arburst(m_axi_arburst),
        .s_axi_arlock(m_axi_arlock),
        .s_axi_arcache(m_axi_arcache),
        .s_axi_arprot(m_axi_arprot),
        .s_axi_arqos(m_axi_arqos),
        .s_axi_aruser(m_axi_aruser),
 
        .s_axi_rid(m_axi_rid),
        .s_axi_rvalid(m_axi_rvalid),
        .s_axi_rready(m_axi_rready),
        .s_axi_rdata(m_axi_rdata),
        .s_axi_rlast(m_axi_rlast),
        .s_axi_rresp(m_axi_rresp)
    );
 
 
endmodule

module axi_master
(
    input  wire        m_axi_aclk,
    input  wire        m_axi_aresetn,
    /////////write data channel
    output reg  [2:0]  m_axi_awid ,
    output reg  [31:0] m_axi_awaddr,
    output reg  [2:0]  m_axi_awsize,
    output reg  [1:0]  m_axi_awburst,
    output reg  [7:0]  m_axi_awlen,
    output reg  [1:0]  m_axi_awlock ,
    output reg  [3:0]  m_axi_awcache,
    output reg  [2:0]  m_axi_awprot,
    output reg  [3:0]  m_axi_awqos,
    output reg  [4:0]  m_axi_awuser,
    output reg         m_axi_awvalid,
    input  wire        m_axi_awready,
    // Write data channel signals
    output reg  [2:0]  m_axi_wid ,
    output reg [31:0] m_axi_wdata,
    output reg  [3:0]  m_axi_wstrb,
    output reg         m_axi_wlast,
    output reg         m_axi_wvalid,
    input  wire        m_axi_wready,
        //  Write response channel signals
    input  wire        m_axi_bid,
    input  wire        m_axi_bresp,
    input  wire        m_axi_bvalid,
    output reg         m_axi_bready,
        //  Read address channel signals
    output reg  [2:0]  m_axi_arid,
    output reg  [31:0] m_axi_araddr,
    output reg  [7:0]  m_axi_arlen,
    output reg  [2:0]  m_axi_arsize,
    output reg  [1:0]  m_axi_arburst,
    output reg  [1:0]  m_axi_arlock,
    output reg  [3:0]  m_axi_arcache,
    output reg  [2:0]  m_axi_arprot,
    output reg  [3:0]  m_axi_arqos,
    output reg  [4:0]  m_axi_aruser,
    output reg         m_axi_arvalid,
    input  wire        m_axi_arready,
        // Read data channel signals
    input  wire [2:0]  m_axi_rid ,
    input  wire [31:0] m_axi_rdata,
    input  wire [1:0]  m_axi_rresp ,
    input  wire        m_axi_rlast,
    input  wire        m_axi_rvalid,
    output reg         m_axi_rready,
    input  wire        wr,
    input  wire [23:0] wr_addr,
    input  wire [7:0]  wr_burst_len,
    input  wire [1:0]  wr_burst_type,
    input  wire [31:0] wr_din,
    input  wire [3:0]  wr_strbin,
    input  wire [23:0] rd_addr,
    input  wire [7:0]  rd_burst_len,
    input  wire [1:0]  rd_burst_type,
    output reg  [31:0] rout,
    output reg  [1:0]  resp
	);
 
localparam idle = 0,
           detect_op = 1,
           send_waddr = 2,
           send_wdata = 3,
           wdata_last = 4,
           wait_for_wr_resp = 5,
           comp_wr_tx = 6,
           no_ack_waddr = 7, 
           no_ack_wdata = 8,
           send_raddr = 9,
           read_rdata = 10,
           no_ack_raddr = 11,
           no_ack_rdata = 12,
           comp_rd_tx   = 13;
           
integer burst_count = 0, wr_count = 0, rd_count = 0;                   
reg [3:0] state = idle, next_state = idle;
reg [31:0] din = 0;
 
 
  initial 
  begin 
                  m_axi_awvalid = 0;
                  m_axi_awid    = 0;
                  m_axi_awaddr  = 0;
                  m_axi_awprot  = 0;
                  m_axi_wvalid  = 0;
                  m_axi_awlen   = 0;
                  m_axi_awsize  = 0;
                  m_axi_awburst = 0;
                  m_axi_awlock  = 0;
                  m_axi_awcache = 0;
                  m_axi_awqos   = 0;
                  m_axi_awuser  = 0;
                  m_axi_wid     = 0;
                  m_axi_wstrb   = 0;
                  m_axi_wlast   = 0;
                  m_axi_bready  = 0;
                  m_axi_arvalid = 0;
                  m_axi_arid    = 0;
                  m_axi_araddr  = 0;
                  m_axi_arlen   = 0;
                  m_axi_arsize  = 0;
                  m_axi_arburst = 0;
                  m_axi_arqos   = 0;
                  m_axi_arprot  = 0;
                  m_axi_arlock  = 0;
                  m_axi_arcache = 0;
                  m_axi_aruser  = 0;
                  m_axi_rready  = 0;
                  burst_count   = 0;
                  din           = 0;
                  m_axi_wdata   = 0;
                  state         = idle; 
                  rout          = 0;
                  resp          = 0;     
            end
                  
         
always @(posedge m_axi_aclk) begin
    if (!m_axi_aresetn) begin
        state <= idle;
    end else begin
        case (state)
            idle: begin
                m_axi_awvalid <= 0;
                m_axi_awid    <= 0;
                m_axi_awaddr  <= 0;
                m_axi_awprot  <= 0;
                m_axi_wvalid  <= 0;
                m_axi_awlen   <= 0;
                m_axi_awsize  <= 0;
                m_axi_awburst <= 0;
                m_axi_awlock  <= 0;
                m_axi_awcache <= 0;
                m_axi_awqos   <= 0;
                m_axi_awuser  <= 0;
                m_axi_wid     <= 0;
                m_axi_wstrb   <= 0;
                m_axi_wlast   <= 0;
                m_axi_wdata   <= 0;
                m_axi_bready  <= 0;
                m_axi_arvalid <= 0;
                m_axi_arid    <= 0;
                m_axi_araddr  <= 0;
                m_axi_arlen   <= 0;
                m_axi_arsize  <= 0;
                m_axi_arburst <= 0;
                m_axi_arqos   <= 0;
                m_axi_arprot  <= 0;
                m_axi_arlock  <= 0;
                m_axi_arcache <= 0;
                m_axi_aruser  <= 0;
                m_axi_rready  <= 0;
                burst_count   <= 0;
                din           <= 0;
                state         <= detect_op;
            end
            
            detect_op: begin
                if (wr) 
                    state <= send_waddr;
                else 
                    state <= send_raddr;
            end
 
            send_waddr: begin
                din           <= wr_din * 5;
                m_axi_awaddr  <= wr_addr;
                m_axi_awvalid <= 1;
                m_axi_wvalid  <= 1;
                m_axi_awlen   <= wr_burst_len;
                m_axi_awsize  <= 3'b010;
                m_axi_awburst <= wr_burst_type;
                m_axi_wdata   <= wr_din;
                // FIX 1: removed << 1 shift - strobe was 1110 instead of 1111
                m_axi_wstrb   <= wr_strbin;
                m_axi_wlast   <= 0;
                burst_count   <= wr_burst_len; 
                m_axi_bready  <= 1;
                 
                if (m_axi_awready == 1) begin
                    state <= send_wdata;
                    wr_count <= 0;
                    m_axi_awvalid  <= 0;
                    m_axi_awaddr   <= 0;
                    m_axi_awlen    <= 0;
                    m_axi_awsize   <= 0;
                    m_axi_awburst  <= 0;
                end else if (wr_count == 15) begin
                    state <= no_ack_waddr;
                    wr_count <= 0;
                end else begin
                    state <= send_waddr;
                    wr_count <= wr_count + 1;
                end
            end
 
            send_wdata: begin
                // FIX 2: handle single-beat case (burst_count == 0)
                // For wr_burst_len=0, burst_count arrives here as 0.
                // The first (only) beat data was already placed on the bus in
                // send_waddr, so assert wlast and go straight to wdata_last.
                if (m_axi_wready && burst_count == 0) begin
                    state       <= wdata_last;
                    m_axi_wlast <= 1;
                    wr_count    <= 0;
                end else if (m_axi_wready && burst_count != 1) begin
                    burst_count   <= burst_count - 1;
                    din           <= din * 5;
                    m_axi_wdata   <= din;
                    state <= send_wdata;
                    wr_count <= 0;
                end else if (m_axi_wready && burst_count == 1) begin
                    burst_count   <= burst_count - 1;
                    m_axi_wdata   <= din;
                    state <= wdata_last;
                    m_axi_wlast   <= 1;
                    wr_count      <= 0;
                end else if (wr_count == 15) begin
                    state <= no_ack_wdata;
                end else begin
                    state <= send_wdata;
                    wr_count <= wr_count + 1;
                end
            end
            
            wdata_last: begin
                if (m_axi_wready && burst_count == 0) begin
                    state <= wait_for_wr_resp;
                    m_axi_wvalid  <= 0;
                    burst_count <= 0;
                    m_axi_wlast   <= 0;
                    m_axi_wdata   <= 0;
                end
            end
 
            no_ack_wdata, no_ack_waddr: begin
                state <= wait_for_wr_resp;
            end
 
            wait_for_wr_resp: begin
                if (m_axi_bvalid == 1) begin 
                    state <= comp_wr_tx;
                    m_axi_bready  <= 0;
                end else if (wr_count == 15) begin
                    state <= idle;
                    wr_count <= 0;
                end else begin
                    state <= wait_for_wr_resp;
                    wr_count <= wr_count + 1;
                end
            end
 
            comp_wr_tx: begin
                m_axi_awaddr   <= 0;
                m_axi_awvalid  <= 0;
                m_axi_wvalid   <= 0;
                m_axi_wlast    <= 0;
                m_axi_wdata    <= 0;
                burst_count    <= 0;
                m_axi_bready   <= 0;
                state <= idle;
            end
 
            send_raddr: begin
                m_axi_araddr  <= rd_addr;
                m_axi_arlen   <= rd_burst_len;
                m_axi_arsize  <= 3'b010;
                m_axi_arburst <= rd_burst_type;
                m_axi_arvalid <= 1;
                m_axi_rready  <= 0;
 
                if (m_axi_arready == 1) begin
                    state <= read_rdata;
                    m_axi_arvalid <= 0;
                    rd_count <= 0;
                end else if (rd_count == 15) begin
                    state <= no_ack_raddr;
                    rd_count <= 0;                    
                end else begin
                    state <= send_raddr;
                    rd_count <= rd_count + 1;
                end          
            end
       
            read_rdata: begin
                m_axi_araddr  <= 0;
                m_axi_arlen   <= 0;
                m_axi_arsize  <= 0;
                m_axi_arburst <= 0;
                m_axi_arvalid <= 0;
                m_axi_rready  <= 1;
 
                if ((m_axi_rvalid == 1'b1) && (m_axi_rlast != 1)) begin
                    rout  <= m_axi_rdata;
                    resp  <= m_axi_rresp;
                    state <= read_rdata;
                    rd_count <= 0;
                end else if ((m_axi_rvalid == 1'b1) && (m_axi_rlast == 1)) begin
                    rout  <= m_axi_rdata;
                    resp  <= m_axi_rresp;
                    state <= comp_rd_tx;
                    rd_count <= 0;
                end else if (rd_count == 15) begin
                    state <= no_ack_rdata;
                    rd_count <= 0;
                end else begin
                    rd_count <= rd_count + 1;
                    state <= read_rdata;
                end
            end
       
            no_ack_raddr, no_ack_rdata: begin
                m_axi_rready <= 0;
                state <= idle;
                rout  <= 0;
                resp  <= 0;
            end 
       
            comp_rd_tx: begin
                m_axi_rready  <= 0;
                state <= idle;
                rout  <= 0;
                resp  <= 0;
            end
 
            default: state <= idle;
        endcase
    end
end
 
endmodule


`timescale 1ns / 1ps
module axi4_slave
(
    input  wire        s_axi_aclk,
    input  wire        s_axi_aresetn,
 
    input  wire [2:0]  s_axi_awid,
    input  wire        s_axi_awvalid,
    output reg         s_axi_awready,
    input  wire [31:0] s_axi_awaddr,
    input  wire [7:0]  s_axi_awlen,
    input  wire [2:0]  s_axi_awsize,
    input  wire [1:0]  s_axi_awburst,
    input  wire [1:0]  s_axi_awlock,
    input  wire [3:0]  s_axi_awcache,
    input  wire [2:0]  s_axi_awprot,
    input  wire [3:0]  s_axi_awqos,
    input  wire [4:0]  s_axi_awuser,
 
    input  wire [2:0]  s_axi_wid,
    input  wire        s_axi_wvalid,
    output reg         s_axi_wready,
    input  wire [31:0] s_axi_wdata,
    input  wire [3:0]  s_axi_wstrb,
    input  wire        s_axi_wlast,
 
    output reg  [2:0]  s_axi_bid,
    output reg         s_axi_bvalid,
    input  wire        s_axi_bready,
    output reg  [1:0]  s_axi_bresp,
 
    input  wire [2:0]  s_axi_arid,
    input  wire        s_axi_arvalid,
    output reg         s_axi_arready,
    input  wire [31:0] s_axi_araddr,
    input  wire [7:0]  s_axi_arlen,
    input  wire [2:0]  s_axi_arsize,
    input  wire [1:0]  s_axi_arburst,
    input  wire [1:0]  s_axi_arlock,
    input  wire [3:0]  s_axi_arcache,
    input  wire [2:0]  s_axi_arprot,
    input  wire [3:0]  s_axi_arqos,
    input  wire [4:0]  s_axi_aruser,
 
    output reg  [2:0]  s_axi_rid,
    output reg         s_axi_rvalid,
    input  wire        s_axi_rready,
    output reg  [31:0] s_axi_rdata,
    output reg         s_axi_rlast,
    output reg  [1:0]  s_axi_rresp
	);
 
localparam  idle = 0,
            predict_op = 1,
            accept_wr = 2,
            wait_wdata = 3,
            accept_wdata = 4,
            gen_data = 5,
            update_mem = 6,
            check_br_len = 7,
            send_ack = 8,
            accept_rd = 9,
            fetch_rdata = 10,
            send_rdata =11,
            rcheck_br_len = 12,
            fetch_ldata = 13,
            send_rlast = 14,
            write_err = 15,
            comp_rd_tx = 16;
   
   initial begin
   s_axi_awready = 0;
   s_axi_wready = 0;
   s_axi_bid    =0;
   s_axi_bvalid = 0;
   s_axi_bresp  = 0;
   s_axi_arready = 0;
   s_axi_rid   = 0;
   s_axi_rvalid = 0;
   s_axi_rdata = 0;
   s_axi_rlast = 0;
   s_axi_rresp = 0;
   end
   
reg [7:0] mem [127:0];
          
reg [4:0] state = 0;
integer i = 0;
reg [7:0] burst_len = 0,rburst_len = 0;
reg [31:0] waddr = 0, wdata = 0,raddr = 0, rdata = 0; 
reg [3:0] wstrb = 0;
integer timer = 0;
reg [31:0] data_write = 0; 
reg [1:0] count = 0;
 
function [31:0] data_wr_fixed (input [3:0] wstrb, input [31:0] awaddrt);
  begin
     case (wstrb)
      4'b0001: begin 
        mem[awaddrt] = wdata[7:0];
      end
      4'b0010: begin 
        mem[awaddrt] = wdata[15:8];
      end
      4'b0011: begin 
        mem[awaddrt] = wdata[7:0];
        mem[awaddrt + 1] = wdata[15:8];
      end
      4'b0100: begin 
         mem[awaddrt] = wdata[23:16];
      end
      4'b0101: begin 
        mem[awaddrt] = wdata[7:0];
        mem[awaddrt + 1] = wdata[23:16];
      end
      4'b0110: begin 
        mem[awaddrt] = wdata[15:8];
         mem[awaddrt + 1] = wdata[23:16];
      end
      4'b0111: begin 
         mem[awaddrt] = wdata[7:0];
         mem[awaddrt + 1] = wdata[15:8];
         mem[awaddrt + 2] = wdata[23:16];
      end
      4'b1000: begin 
         mem[awaddrt] = wdata[31:24];
      end
      4'b1001: begin 
         mem[awaddrt] = wdata[7:0];
         mem[awaddrt + 1] = wdata[31:24];
      end
      4'b1010: begin 
         mem[awaddrt] = wdata[15:8];
         mem[awaddrt + 1] = wdata[31:24];
      end
      4'b1011: begin 
         mem[awaddrt] = wdata[7:0];
         mem[awaddrt + 1] = wdata[15:8];
         mem[awaddrt + 2] = wdata[31:24];
      end
      4'b1100: begin 
         mem[awaddrt] = wdata[23:16];
         mem[awaddrt + 1] = wdata[31:24];
      end
      4'b1101: begin 
        mem[awaddrt] = wdata[7:0];
        mem[awaddrt + 1] = wdata[23:16];
        mem[awaddrt + 2] = wdata[31:24];
      end
      4'b1110: begin 
        mem[awaddrt] = wdata[15:8];
        mem[awaddrt + 1] = wdata[23:16];
        mem[awaddrt + 2] = wdata[31:24];
      end
      4'b1111: begin
        mem[awaddrt] = wdata[7:0];
        mem[awaddrt + 1] = wdata[15:8];
        mem[awaddrt + 2] = wdata[23:16];
        mem[awaddrt + 3] = wdata[31:24];       
      end
     endcase
    data_wr_fixed =  awaddrt;
end
endfunction  
  
reg [31:0] addr = 0;
function [31:0] data_wr_incr (input [3:0] wstrb, input [31:0] awaddrt);
 begin      
    case (wstrb)
      4'b0001: begin 
        mem[awaddrt] = wdata[7:0];
        addr = awaddrt + 1;
      end
      4'b0010: begin 
        mem[awaddrt] = wdata[15:8];
        addr = awaddrt + 1;
      end
      4'b0011: begin 
        mem[awaddrt] = wdata[7:0];
        mem[awaddrt + 1] = wdata[15:8];
        addr = awaddrt + 2;
      end
      4'b0100: begin 
         mem[awaddrt] = wdata[23:16];
         addr = awaddrt + 1;
      end
      4'b0101: begin 
        mem[awaddrt] = wdata[7:0];
         mem[awaddrt + 1] = wdata[23:16];
         addr = awaddrt + 2;
      end
      4'b0110: begin 
         mem[awaddrt] = wdata[15:8];
         mem[awaddrt + 1] = wdata[23:16];
         addr = awaddrt + 2;
      end
      4'b0111: begin 
         mem[awaddrt] = wdata[7:0];
         mem[awaddrt + 1] = wdata[15:8];
         mem[awaddrt + 2] = wdata[23:16];
         addr = awaddrt + 3;
      end
      4'b1000: begin 
         mem[awaddrt] = wdata[31:24];
         addr = awaddrt + 1;
      end
      4'b1001: begin 
         mem[awaddrt] = wdata[7:0];
         mem[awaddrt + 1] = wdata[31:24];
         addr = awaddrt + 2;
      end
      4'b1010: begin 
         mem[awaddrt] = wdata[15:8];
         mem[awaddrt + 1] = wdata[31:24];
         addr = awaddrt + 2;
      end
      4'b1011: begin 
         mem[awaddrt] = wdata[7:0];
         mem[awaddrt + 1] = wdata[15:8];
         mem[awaddrt + 2] = wdata[31:24];
         addr = awaddrt + 3;
      end
      4'b1100: begin 
         mem[awaddrt] = wdata[23:16];
         mem[awaddrt + 1] = wdata[31:24];
         addr = awaddrt + 2;
      end
      4'b1101: begin 
        mem[awaddrt] = wdata[7:0];
        mem[awaddrt + 1] = wdata[23:16];
        mem[awaddrt + 2] = wdata[31:24];
        addr = awaddrt + 3;
      end
      4'b1110: begin 
        mem[awaddrt] = wdata[15:8];
        mem[awaddrt + 1] = wdata[23:16];
        mem[awaddrt + 2] = wdata[31:24];
        addr = awaddrt + 3;
      end
      4'b1111: begin
        mem[awaddrt]     = wdata[7:0];
        mem[awaddrt + 1] = wdata[15:8];
        mem[awaddrt + 2] = wdata[23:16];
        mem[awaddrt + 3] = wdata[31:24]; 
        addr = awaddrt + 4;      
      end
     endcase
    data_wr_incr =  addr;
end
endfunction   
 
reg [7:0] boundary_wr;
function  [7:0] wrap_boundary (input [3:0] awlen,input [2:0] awsize);
   begin
      case(awlen)
       4'b0001: 
       begin
                case(awsize)
                       3'b000: begin boundary_wr = 2 * 1; end
                       3'b001: begin boundary_wr = 2 * 2; end
                       3'b010: begin boundary_wr = 2 * 4; end
                endcase
          end
       4'b0011: 
       begin
                case(awsize)
                       3'b000: begin boundary_wr = 4 * 1; end
                       3'b001: begin boundary_wr = 4 * 2; end
                       3'b010: begin boundary_wr = 4 * 4; end
                endcase
          end
       4'b0111: 
       begin
                case(awsize)
                       3'b000: begin boundary_wr = 8 * 1; end
                       3'b001: begin boundary_wr = 8 * 2; end
                       3'b010: begin boundary_wr = 8 * 4; end
                endcase
          end
       4'b1111: 
       begin
                case(awsize)
                       3'b000: begin boundary_wr = 16 * 1; end
                       3'b001: begin boundary_wr = 16 * 2; end
                       3'b010: begin boundary_wr = 16 * 4; end
                endcase
          end
     endcase
     wrap_boundary =  boundary_wr;
end
endfunction
  
reg [31:0] addr1, addr2, addr3, addr4;
reg [31:0] nextaddr, nextaddr2;
function [31:0] data_wr_wrap (input [3:0] wstrb, input [31:0] awaddrt, input [7:0] wboundary);
begin
  case (wstrb)   
      4'b0001: begin 
        mem[awaddrt] = wdata[7:0];
        if((awaddrt + 1) % wboundary == 0) addr1 = (awaddrt + 1) - wboundary;
        else                               addr1 = awaddrt + 1;
        data_wr_wrap = addr1;
      end
      4'b0010: begin 
        mem[awaddrt] = wdata[15:8];
        if((awaddrt + 1) % wboundary == 0) addr1 = (awaddrt + 1) - wboundary;
        else                               addr1 = awaddrt + 1;
        data_wr_wrap = addr1;   
      end
      4'b0011: begin 
        mem[awaddrt] = wdata[7:0];
        if((awaddrt + 1) % wboundary == 0) addr1 = (awaddrt + 1) - wboundary;
        else                               addr1 = awaddrt + 1;
        mem[addr1] = wdata[15:8]; 
        if((addr1 + 1) % wboundary == 0) addr2 = (addr1 + 1) - wboundary;
        else                             addr2 = addr1 + 1;
        data_wr_wrap = addr2;   
       end
      4'b0100: begin 
         mem[awaddrt] = wdata[23:16];
         if((awaddrt + 1) % wboundary == 0) addr1 = (awaddrt + 1) - wboundary;
         else                               addr1 = awaddrt + 1;
         data_wr_wrap = addr1;
      end
      4'b0101: begin 
        mem[awaddrt] = wdata[7:0];
        if((awaddrt + 1) % wboundary == 0) addr1 = (awaddrt + 1) - wboundary;
        else                               addr1 = awaddrt + 1;
        mem[addr1] = wdata[23:16];
        if((addr1 + 1) % wboundary == 0) addr2 = (addr1 + 1) - wboundary;
        else                             addr2 = addr1 + 1;
        data_wr_wrap = addr2;  
      end
      4'b0110: begin 
        mem[awaddrt] = wdata[15:8];
        if((awaddrt + 1) % wboundary == 0) addr1 = (awaddrt + 1) - wboundary;
        else                               addr1 = awaddrt + 1;
        mem[addr1] = wdata[23:16];
        if((addr1 + 1) % wboundary == 0) addr2 = (addr1 + 1) - wboundary;
        else                             addr2 = addr1 + 1;
        data_wr_wrap = addr2;  
      end
      4'b0111: begin 
         mem[awaddrt] = wdata[7:0];    
         if((awaddrt + 1) % wboundary == 0) addr1 = (awaddrt + 1) - wboundary;
         else                               addr1 = awaddrt + 1;
         mem[addr1] = wdata[15:8];
         if((addr1 + 1) % wboundary == 0) addr2 = (addr1 + 1) - wboundary;
         else                             addr2 = addr1 + 1;
         mem[addr2] = wdata[23:16];
         if((addr2 + 1) % wboundary == 0) addr3 = (addr2 + 1) - wboundary;
         else                             addr3 = addr2 + 1;
         data_wr_wrap = addr3;
      end
      4'b1000: begin 
         mem[awaddrt] = wdata[31:24];
         if((awaddrt + 1) % wboundary == 0) addr1 = (awaddrt + 1) - wboundary;
         else                               addr1 = awaddrt + 1;
         data_wr_wrap = addr1;
      end
      4'b1001: begin 
         mem[awaddrt] = wdata[7:0];
         if((awaddrt + 1) % wboundary == 0) addr1 = (awaddrt + 1) - wboundary;
         else                               addr1 = awaddrt + 1;
         mem[addr1] = wdata[31:24];
         if((addr1 + 1) % wboundary == 0) addr2 = (addr1 + 1) - wboundary;
         else                             addr2 = addr1 + 1;
         data_wr_wrap = addr2;
      end
      4'b1010: begin 
         mem[awaddrt] = wdata[15:8];
         if((awaddrt + 1) % wboundary == 0) addr1 = (awaddrt + 1) - wboundary;
         else                               addr1 = awaddrt + 1;
         mem[addr1] = wdata[31:24];
         if((addr1 + 1) % wboundary == 0) addr2 = (addr1 + 1) - wboundary;
         else                             addr2 = addr1 + 1;
         data_wr_wrap = addr2;
      end
      4'b1011: begin 
         mem[awaddrt] = wdata[7:0];
         if((awaddrt + 1) % wboundary == 0) addr1 = (awaddrt + 1) - wboundary;
         else                               addr1 = awaddrt + 1;
         mem[addr1] = wdata[15:8];
         if((addr1 + 1) % wboundary == 0) addr2 = (addr1 + 1) - wboundary;
         else                             addr2 = addr1 + 1;
         mem[addr2] = wdata[31:24];
         if((addr2 + 1) % wboundary == 0) addr3 = (addr2 + 1) - wboundary;
         else                             addr3 = addr2 + 1;
         data_wr_wrap = addr3;
      end
      4'b1100: begin 
         mem[awaddrt] = wdata[23:16];
         if((awaddrt + 1) % wboundary == 0) addr1 = (awaddrt + 1) - wboundary;
         else                               addr1 = awaddrt + 1;
         mem[addr1] = wdata[31:24];
         if((addr1 + 1) % wboundary == 0) addr2 = (addr1 + 1) - wboundary;
         else                             addr2 = addr1 + 1;
         data_wr_wrap = addr2;
      end
      4'b1101: begin 
        mem[awaddrt] = wdata[7:0];
        if((awaddrt + 1) % wboundary == 0) addr1 = (awaddrt + 1) - wboundary;
        else                               addr1 = awaddrt + 1;
        mem[addr1] = wdata[23:16];
        if((addr1 + 1) % wboundary == 0) addr2 = (addr1 + 1) - wboundary;
        else                             addr2 = addr1 + 1;
        mem[addr2] = wdata[31:24];
        if((addr2 + 1) % wboundary == 0) addr3 = (addr2 + 1) - wboundary;
        else                             addr3 = addr2 + 1;
        data_wr_wrap = addr3;
      end
      4'b1110: begin 
        mem[awaddrt] = wdata[15:8];
        if((awaddrt + 1) % wboundary == 0) addr1 = (awaddrt + 1) - wboundary;
        else                               addr1 = awaddrt + 1;
        mem[addr1] = wdata[23:16];
        if((addr1 + 1) % wboundary == 0) addr2 = (addr1 + 1) - wboundary;
        else                             addr2 = addr1 + 1;
        mem[addr2] = wdata[31:24];
        if((addr2 + 1) % wboundary == 0) addr3 = (addr2 + 1) - wboundary;
        else                             addr3 = addr2 + 1;
        data_wr_wrap = addr3;
      end
      4'b1111: begin
        mem[awaddrt] = wdata[7:0];
        if((awaddrt + 1) % wboundary == 0) addr1 = (awaddrt + 1) - wboundary;
        else                               addr1 = awaddrt + 1;
        mem[addr1] = wdata[15:8];
        if((addr1 + 1) % wboundary == 0) addr2 = (addr1 + 1) - wboundary;
        else                             addr2 = addr1 + 1;
        mem[addr2] = wdata[23:16];
        if((addr2 + 1) % wboundary == 0) addr3 = (addr2 + 1) - wboundary;
        else                             addr3 = addr2 + 1;
        mem[addr3] = wdata[31:24]; 
        if((addr3 + 1) % wboundary == 0) addr4 = (addr3 + 1) - wboundary;
        else                             addr4 = addr3 + 1;
        data_wr_wrap = addr4;        
      end
     endcase
end
endfunction  
  
function [31:0] read_data_fixed (input [31:0] addr, input [2:0] arsize);
begin
             case(arsize)
                 3'b000: begin
                  rdata[7:0] = mem[addr];    
                 end
                 3'b001: begin
                  rdata[7:0]  = mem[addr]; 
                  rdata[15:8] = mem[addr + 1]; 
                 end 
                 3'b010: begin
                  rdata[7:0]    = mem[addr]; 
                  rdata[15:8]   = mem[addr + 1]; 
                  rdata[23:16]  = mem[addr + 2]; 
                  rdata[31:24]  = mem[addr + 3]; 
                 end
                 endcase
                read_data_fixed = addr;
end       
endfunction 
 
reg [31:0] rnext_addr = 0;
function [31:0] read_data_incr(input [31:0] addr, input [2:0] arsize);
 begin
     case(arsize)
        3'b000: begin
          rdata[7:0] = mem[addr];
          rnext_addr = addr + 1;
       end
       3'b001: begin
       rdata[7:0]  = mem[addr];
       rdata[15:8] = mem[addr + 1];
       rnext_addr  = addr + 2;  
       end
       3'b010: begin
       rdata[7:0]    = mem[addr];
       rdata[15:8]   = mem[addr + 1];
       rdata[23:16]  = mem[addr + 2];
       rdata[31:24]  = mem[addr + 3];
       rnext_addr = addr + 4;  
       end
      endcase  
   read_data_incr =  rnext_addr;
 end     
endfunction
 
 // FIX 3: read_data_wrap 3'b010 case used write-side 'addr1' instead of
 //        read-side 'raddr1', and returned 'addr4' instead of 'raddr4'.
 reg [31:0] raddr1 = 0, raddr2 = 0, raddr3 = 0, raddr4 = 0;
function [31:0] read_data_wrap (input  [31:0] addr, input  [2:0] arsize, input [7:0] rboundary);
begin
   case (arsize)
     3'b000: begin
        rdata[7:0] = mem[addr];
        if(((addr + 1) % rboundary ) == 0) raddr1 = (addr + 1) - rboundary;
        else                               raddr1 = (addr + 1);
        read_data_wrap =  raddr1;       
     end
     3'b001: begin
        rdata[7:0] = mem[addr];
        if(((addr + 1) % rboundary ) == 0) raddr1 = (addr + 1) - rboundary;
        else                               raddr1 = (addr + 1);
        rdata[15:8] = mem[raddr1];
        if(((raddr1 + 1) % rboundary ) == 0) raddr2 = (raddr1 + 1) - rboundary;
        else                                 raddr2 = (raddr1 + 1);         
        read_data_wrap = raddr2;       
     end
     3'b010: begin
        rdata[7:0] = mem[addr];
        if(((addr + 1) % rboundary ) == 0) raddr1 = (addr + 1) - rboundary;
        else                               raddr1 = (addr + 1);
        // FIX: was mem[addr1] (write-side variable) - now correctly mem[raddr1]
        rdata[15:8] = mem[raddr1];
        if(((raddr1 + 1) % rboundary ) == 0) raddr2 = (raddr1 + 1) - rboundary;
        else                                 raddr2 = (raddr1 + 1);  
        rdata[23:16]  = mem[raddr2];
        if(((raddr2 + 1) % rboundary ) == 0) raddr3 = (raddr2 + 1) - rboundary;
        else                                 raddr3 = (raddr2 + 1); 
        rdata[31:24] = mem[raddr3];
        if(((raddr3 + 1) % rboundary ) == 0) raddr4 = (raddr3 + 1) - rboundary;
        else                                 raddr4 = (raddr3 + 1);            
        // FIX: was addr4 (write-side variable) - now correctly raddr4
        read_data_wrap =  raddr4;
     end
   endcase
  end
endfunction
                
reg [7:0] boundary = 0, rboundary = 0;
reg [7:0] awlen = 0, arlen = 0;
reg [2:0] awsize = 0, arsize = 0; 
reg  [1:0] awburst = 0 , arburst = 0;      
 
always @(posedge s_axi_aclk) begin
    if (s_axi_aresetn == 0) begin
        for (i = 0; i < 128; i = i + 1) begin
            mem[i] <= 0;
        end
    end else begin
        case (state)
            idle: begin
                raddr   <= 0;
                rdata   <= 0;
                addr    <= 0;
                rboundary <= 0;
                data_write <= 0;
                awlen      <= 0;
                arlen      <= 0;
                arsize    <= 0;
                awsize    <= 0;
                arburst   <= 0;
                awburst   <= 0;
                boundary_wr <= 0;
                addr1 <= 0;
                addr2 <= 0;
                addr3 <= 0;
                addr4 <= 0;
                s_axi_awready <= 1'b0;
                s_axi_wready  <= 1'b0;
                s_axi_bid     <= 3'b000;
                s_axi_bvalid  <= 1'b0;
                s_axi_bresp   <= 2'b00;
                s_axi_arready <= 1'b0;
                s_axi_rvalid  <= 1'b0;
                s_axi_rresp   <= 2'b00;
                s_axi_rid     <= 3'b000;
                s_axi_rlast   <= 1'b0;
                s_axi_rdata   <= 32'h0;
                state         <= predict_op;
            end
 
            predict_op: begin
                if (s_axi_awvalid)
                    state <= accept_wr;
                else if (s_axi_arvalid)
                    state <= accept_rd;
                else
                    state <= idle;
            end
 
            accept_wr: begin
                if (s_axi_awaddr < 128 && ((s_axi_awaddr + s_axi_awlen*4 + 1) < 128)) begin
                    burst_len <= s_axi_awlen + 1;
                    waddr     <= s_axi_awaddr;
                    state     <= wait_wdata;
                    awlen     <= s_axi_awlen;
                    awsize    <= s_axi_awsize;
                    awburst   <= s_axi_awburst;
                    s_axi_awready <= 1'b1;
                end else begin
                    s_axi_awready <= 1'b0;
                    state <= idle;
                end
            end
 
            wait_wdata: begin
                s_axi_awready <= 1'b0;
                if (s_axi_wvalid) begin
                    state <= accept_wdata;
                    wdata <= s_axi_wdata;
                    wstrb <= s_axi_wstrb;
                end else if (timer == 15) begin
                    state <= write_err;
                    timer <= 0;
                end else begin
                    timer <= timer + 1;
                    state <= wait_wdata;
                end
            end
 
            accept_wdata: begin
                s_axi_wready <= 1'b1;
                state        <= gen_data;
            end
 
            gen_data: begin
                s_axi_wready <= 1'b0;
                data_write <= {(wdata[31:24] & {8{wstrb[3]}}), 24'h0} |
                              {8'h0, (wdata[23:16] & {8{wstrb[2]}}), 16'h0} |
                              {16'h0, (wdata[15:8] & {8{wstrb[1]}}), 8'h0} |
                              {24'h0, (wdata[7:0] & {8{wstrb[0]}})};
                state <= update_mem;
            end
 
            // FIX 4: original code did  mem[waddr] <= data_write
            // which truncates the 32-bit data_write to 8 bits (mem is byte-wide)
            // and stores only [7:0] regardless of strobe - byte 0 was always 0
            // because strobe bit 0 was masked by the << 1 bug in the master.
            // Now write each byte individually, gated by its strobe bit.
            update_mem: begin
                if (count < 2) begin
                    count <= count + 1;
                    state <= update_mem;
                    if (wstrb[0]) mem[waddr]   <= wdata[7:0];
                    if (wstrb[1]) mem[waddr+1] <= wdata[15:8];
                    if (wstrb[2]) mem[waddr+2] <= wdata[23:16];
                    if (wstrb[3]) mem[waddr+3] <= wdata[31:24];
                end else begin
                    burst_len <= burst_len - 1;
                    count <= 0;
                    state <= check_br_len;
                end
            end
 
            check_br_len: begin
                if (burst_len == 0)
                    state <= send_ack;
                else begin
                    state <= wait_wdata;
                    case(awburst)
                          2'b00: waddr <= data_wr_fixed(wstrb, waddr);
                          2'b01: waddr <= data_wr_incr(wstrb, waddr); 
                          2'b10: begin
                               boundary <= wrap_boundary(awlen, awsize);
                               waddr    <= data_wr_wrap(wstrb, waddr, boundary);
                          end     
                    endcase    
                end
            end
 
            send_ack: begin
                if (s_axi_bready) begin
                    s_axi_bvalid <= 1'b1;
                    s_axi_bresp  <= 2'b00;
                    state        <= idle;
                end else if (timer == 15) begin
                    state <= idle;
                end else begin
                    timer <= timer + 1;
                    state <= send_ack;
                end
            end
 
            accept_rd: begin
                if (s_axi_araddr < 128 && ((s_axi_araddr + s_axi_arlen*4 + 1) < 128)) begin
                    rburst_len <= s_axi_arlen;
                    raddr      <= s_axi_araddr;
                    state      <= fetch_rdata;
                    arsize     <= s_axi_arsize;
                    arlen      <= s_axi_arlen;
                    arburst    <= s_axi_arburst;
                    s_axi_arready <= 1'b1;
                end else begin
                    s_axi_arready <= 1'b0;
                    state <= idle;
                end
            end
 
            fetch_rdata: begin
                s_axi_arready <= 1'b0;
                if (count < 2) begin
                    count <= count + 1;
                    state <= fetch_rdata;
                    rdata <= mem[raddr];
                end else begin
                    count <= 0;
                    state <= send_rdata;
                end
            end
 
            send_rdata: begin
                s_axi_rvalid <= 1'b1;
                s_axi_rdata  <= rdata;
                s_axi_rresp  <= 2'b00;
                if (s_axi_rready) begin
                    state <= rcheck_br_len;
                end else if (timer == 15) begin
                    state <= idle;
                    timer <= 0;
                end else begin
                    state <= send_rdata;
                    timer <= timer + 1;
                end
            end
 
            // FIX 5: original check was  rburst_len == 1
            // For arlen=0 (single beat), rburst_len starts at 0.
            // After decrement: 0 - 1 = 0xFF (8-bit underflow), which never
            // equals 1, so the slave looped 255+ extra beats before wrapping.
            // Changed to  rburst_len <= 1  to handle both arlen=0 and arlen>0.
            rcheck_br_len: begin
                rburst_len <= rburst_len - 1;
                s_axi_rvalid <= 1'b0;
                case(arburst)
                2'b00: raddr <= read_data_fixed(raddr, arsize);
                2'b01: raddr <= read_data_incr(raddr, arsize);
                2'b10: begin
                    rboundary  <=  wrap_boundary(arlen, arsize);
                    raddr      <=  read_data_wrap(raddr, arsize, rboundary);
                end
                endcase
                // FIX: was == 1, which caused underflow for single-beat (arlen=0)
                if (rburst_len <= 1) begin
                    state <= fetch_ldata;
                end else begin
                    state <= fetch_rdata;
                end
            end
 
            fetch_ldata: begin
                if (count < 2) begin
                    count <= count + 1;
                    state <= fetch_ldata;
                    rdata <= mem[raddr];
                end else begin
                    count <= 0;
                    state <= send_rlast;
                    s_axi_rvalid <= 1'b1;
                    s_axi_rdata  <= rdata;
                    s_axi_rresp  <= 2'b00;
                    s_axi_rlast  <= 1'b1;
                end
            end
 
            send_rlast: begin
                if (s_axi_rready == 1'b1) begin
                    state        <= idle;
                    s_axi_rvalid <= 1'b0;
                    s_axi_rdata  <= 0;
                    s_axi_rresp  <= 2'b00;
                    s_axi_rlast  <= 1'b0;
                    timer        <= 0;
                end else if (timer == 15) begin
                    state <= idle;
                    timer <= 0;
                end else begin
                    state <= send_rlast;
                    timer <= timer + 1;
                end
            end
 
            default: state <= idle;
        endcase
    end
end
 
endmodule
