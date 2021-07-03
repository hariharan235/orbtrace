from nmigen                  import *
from .dbgIF                  import DBGIF

# Principle of operation
# ======================
#
# This module takes frames from the stream handler, parses them and sends them to the dbgif below for
# processing. In general this layer avoids doing any manipulation of the line, that is all handled
# below, with the intention of being able to replace cmsis-dap with another dap controller if
# needed.
#
# Communication with the dbgif is via a register and flag mechanism. Registers are filled with the
# appropriate information, then 'go' is set. When the dbgif accepts the command it drops 'done'
# and this layer can then release 'go'. When the command finishes 'done' is set true again.
#
# Default configuration information
# =================================

DAP_CONNECT_DEFAULT      = 1                # Default connect is SWD
DAP_PROTOCOL_STRING_LEN  = 5
DAP_PROTOCOL_STRING      = Cat(C(DAP_PROTOCOL_STRING_LEN+1,8),C(ord('2'),8),C(ord('.'),8),C(ord('1'),8),C(ord('.'),8),C(ord('0'),8),C(0,8)) # Protocol version V2.1.0
DAP_VERSION_STRING_LEN   = 4
DAP_VERSION_STRING       = Cat(C(DAP_VERSION_STRING_LEN+1,8),C(0x31,8),C(0x2e,8),C(0x30,8),C(0x30,8),C(0,8))
DAP_CAPABILITIES         = 0x03             # JTAG and SWD Debug
DAP_TD_TIMER_FREQ        = 0x3B9ACA00       # 1uS resolution timer
DAP_MAX_PACKET_COUNT     = 1                # 1 max packet count
DAP_V1_MAX_PACKET_SIZE   = 64
DAP_V2_MAX_PACKET_SIZE   = 511
MAX_MSG_LEN              = DAP_V2_MAX_PACKET_SIZE

# CMSIS-DAP Protocol Messages
# ===========================

DAP_Info                 = 0x00
DAP_HostStatus           = 0x01
DAP_Connect              = 0x02
DAP_Disconnect           = 0x03
DAP_TransferConfigure    = 0x04
DAP_Transfer             = 0x05
DAP_TransferBlock        = 0x06
DAP_TransferAbort        = 0x07
DAP_WriteABORT           = 0x08
DAP_Delay                = 0x09
DAP_ResetTarget          = 0x0a
DAP_SWJ_Pins             = 0x10
DAP_SWJ_Clock            = 0x11
DAP_SWJ_Sequence         = 0x12
DAP_SWD_Configure        = 0x13
DAP_JTAG_Sequence        = 0x14
DAP_JTAG_Configure       = 0x15
DAP_JTAG_IDCODE          = 0x16
DAP_SWO_Transport        = 0x17
DAP_SWO_Mode             = 0x18
DAP_SWO_Baudrate         = 0x19
DAP_SWO_Control          = 0x1a
DAP_SWO_Status           = 0x1b
DAP_SWO_Data             = 0x1c
DAP_SWD_Sequence         = 0x1d
DAP_SWO_ExtendedStatus   = 0x1e
DAP_ExecuteCommands      = 0x7f

DAP_QueueCommands        = 0x7e
DAP_Invalid              = 0xff

# Commands to the dbgIF
# =====================

CMD_RESET                = 0
CMD_PINS_WRITE           = 1
CMD_TRANSACT             = 2
CMD_SET_SWD              = 3
CMD_SET_JTAG             = 4
CMD_SET_SWJ              = 5
CMD_SET_JTAG_CFG         = 6
CMD_SET_CLK              = 7
CMD_SET_SWD_CFG          = 8
CMD_WAIT                 = 9
CMD_CLR_ERR              = 10
CMD_SET_RST_TMR          = 11
CMD_SET_TFR_CFG          = 12
CMD_JTAG_GET_ID          = 13
CMD_JTAG_RESET           = 14

# TODO/Done
# =========

# DAP_Info               : Done
# DAP_Hoststatus         : Done (But not tied to h/w)
# DAP_Connect            : Done
# DAP_Disconnect         : Done
# DAP_WriteABORT         : Done
# DAP_Delay              : Done
# DAP_ResetTarget        : Done
# DAP_SWJ_Pins           : Done
# DAP_SWJ_Clock          : Done
# DAP_SWJ_Sequence       : Done
# DAP_SWD_Configure      : Done
# DAP_SWD_Sequence       :
# DAP_SWO_Transport      : Not implemented
# DAP_SWO_Mode           : Not implemented
# DAP_SWO_Baudrate       : Not implemented
# DAP_SWO_Control        : Not implemented
# DAP_SWO_Status         : Not implemented
# DAP_SWO_ExtendedStatus : Not implemented
# DAP_SWO_Data           : Not implemented
# DAP_JTAG_Sequence      :
# DAP_JTAG_Configure     :
# DAP_JTAG_IDCODE        :
# DAP_Transfer_Configure : Done
# DAP_Transfer           : Done (Masking done, not tested)
# DAP_TransferBlock      : Done
# DAP_TransferAbort      : Done
# DAP_ExecuteCommands    :
# DAP_QueueCommands      :

# This is the RAM used to store responses before they are sent back to the host
# =============================================================================

class WideRam(Elaboratable):
    def __init__(self):
        self.adr   = Signal(range((MAX_MSG_LEN//4)))
        self.dat_r = Signal(32)
        self.dat_w = Signal(32)
        self.we    = Signal()
        self.mem   = Memory(width=32, depth=MAX_MSG_LEN//4)

    def elaborate(self, platform):
        m = Module()
        m.submodules.rdport = rdport = self.mem.read_port()
        m.submodules.wrport = wrport = self.mem.write_port()
        m.d.comb += [
            rdport.addr.eq(self.adr),
            wrport.addr.eq(self.adr),
            self.dat_r.eq(rdport.data),
            wrport.data.eq(self.dat_w),
            wrport.en.eq(self.we),
        ]
        return m

# This is the CMSIS-DAP handler itself
# ====================================

class CMSIS_DAP(Elaboratable):
    def __init__(self, streamIn, streamOut, dbgpins, v2Indication):
        # Canary
        self.can          = Signal()

        # External interface
        self.running      = Signal()       # Flag for if target is running
        self.connected    = Signal()       # Flag for if target is connected

        self.isV2         = v2Indication
        self.streamIn     = streamIn
        self.streamOut    = streamOut
        self.rxBlock      = Signal( 7*8 )  # Longest message we pickup is 6 bytes + command
        self.rxLen        = Signal(3)      # Rxlen to pick up
        self.rxedLen      = Signal(3)      # Rxlen picked up so far
        self.swjbits      = Signal(8)      # Number of bits of SWJ remaining outstanding

        self.txBlock      = Signal( 14*8 ) # Response to be returned
        self.txLen        = Signal(range(MAX_MSG_LEN))     # Length of response to be returned
        self.txedLen      = Signal(range(MAX_MSG_LEN))     # Length of response that has been returned so far
        self.busy         = Signal()       # Indicator that we can't receive stream traffic at the moment

        self.txb          = Signal(5)      # State of various orthogonal state machines

        # Support for SWJ_Sequence
        self.bitcount     = Signal(3)      # Bitcount in transmission sequence

        # Support for JTAG_Sequence
        self.tmsValue     = Signal()       # TMS value while performing JTAG sequence
        self.tdoCapture   = Signal()       # Are we capturing TDO when performing JTAG sequence
        self.tdiData      = Signal(8)      # TDI being sent out
        self.tdoCount     = Signal(4)      # Count of tdi bits being sent
        self.tdiCount     = Signal(4)      # Count of tdi bits being received
        self.seqCount     = Signal(8)      # Number of sequences that follow
        self.tckCycles    = Signal(6)      # Number of tckCycles in this sequence
        self.tdotgt       = Signal(7)      # Number of tdo cycles to collect (note the extra bit)
        self.pendingTx    = Signal(8)      # Next octet to be sent out of streamIn
        self.tdoBuild     = Signal(8)      # Return value being built

        # Support for DAP_Transfer
        self.dapIndex     = Signal(8)      # Index of selected JTAG device
        self.transferCount= Signal(16)     # Number of transfers 1..65535

        self.mask         = Signal(32)     # Match mask register

        self.retries      = Signal(16)     # Retry counter for WAIT
        self.matchretries = Signal(16)     # Retry counter for Value Matching

        self.tfrReq       = Signal(8)      # Transfer request from controller
        self.tfrData      = Signal(32)     # Transfer data from controller

        # CMSIS-DAP Configuration info
        self.ndev         = Signal(8)      # Number of devices in signal chain
        self.irlength     = Signal(8)      # JTAG IR register length for each device

        self.waitRetry    = Signal(16)     # Number of transfer retries after WAIT response
        self.matchRetry   = Signal(16)     # Number of retries on reads with Value Match in DAP_Transfer

        self.dbgpins      = dbgpins
    # -------------------------------------------------------------------------------------
    def RESP_Invalid(self, m):
        # Simply transmit an 'invalid' packet back
        m.d.sync += [ self.txBlock.word_select(0,8).eq(C(DAP_Invalid,8)), self.txLen.eq(1), self.busy.eq(1) ]
        m.next = 'RESPOND'
    # -------------------------------------------------------------------------------------
    def RESP_Info(self, m):
        # <b:0x00> <b:requestId>
        # Transmit requested information packet back
        m.next = 'RESPOND'

        with m.Switch(self.rxBlock.word_select(1,8)):
            # These cases are not implemented in this firmware
            # Get the Vendor ID, Product ID, Serial Number, Target Device Vendor, Target Device Name,
            # Target Board Vendor, Target Board Name
            with m.Case(0x01, 0x02, 0x03, 0x05, 0x06, 0x07, 0x08):
                m.d.sync += [ self.txLen.eq(2), self.txBlock[8:16].eq(Cat(C(0,8))) ]
            with m.Case(0x04): # Get the CMSIS-DAP Firmware Version (string)
                m.d.sync += [ self.txLen.eq(3+DAP_PROTOCOL_STRING_LEN),
                              self.txBlock.bit_select(8,8+(2+DAP_PROTOCOL_STRING_LEN)*8).eq(DAP_PROTOCOL_STRING) ]
            with m.Case(0x09): # Get the Product Firmware version (string)
                m.d.sync += [ self.txLen.eq(3+DAP_VERSION_STRING_LEN),
                              self.txBlock.bit_select(8,8+(2+DAP_VERSION_STRING_LEN)*8).eq(DAP_VERSION_STRING)  ]
            with m.Case(0xF0): # Get information about the Capabilities (BYTE) of the Debug Unit
                m.d.sync+=[self.txLen.eq(3), self.txBlock[8:24].eq(Cat(C(1,8),C(DAP_CAPABILITIES,8)))]
            with m.Case(0xF1): # Get the Test Domain Timer parameter information
                m.d.sync+=[self.txLen.eq(6), self.txBlock[8:56].eq(Cat(C(8,8),C(DAP_TD_TIMER_FREQ,32)))]
            with m.Case(0xFD): # Get the SWO Trace Buffer Size (WORD)
                m.d.sync+=[self.txLen.eq(6), self.txBlock[8:48].eq(Cat(C(4,8),C(0,32)))]
            with m.Case(0xFE): # Get the maximum Packet Count (BYTE)
                m.d.sync+=[self.txLen.eq(6), self.txBlock[8:24].eq(Cat(C(1,8),C(DAP_MAX_PACKET_COUNT,8)))]
            with m.Case(0xFF): # Get the maximum Packet Size (SHORT).
                with m.If(self.isV2):
                    m.d.sync+=[self.txLen.eq(6), self.txBlock[8:32].eq(Cat(C(2,8),C(DAP_V2_MAX_PACKET_SIZE,16)))]
                with m.Else():
                    m.d.sync+=[self.txLen.eq(6), self.txBlock[8:32].eq(Cat(C(2,8),C(DAP_V1_MAX_PACKET_SIZE,16)))]
            with m.Default():
                self.RESP_Invalid(m)
    # -------------------------------------------------------------------------------------
    def RESP_Not_Implemented(self, m):
        m.d.sync += self.txBlock.word_select(1,8).eq(C(0xff,8))
        m.next = 'RESPOND'
    # -------------------------------------------------------------------------------------
    def RESP_HostStatus(self, m):
        # <b:0x01> <b:type> <b:status>
        # Set LEDs for condition of debugger
        m.next = 'RESPOND'

        with m.Switch(self.rxBlock.word_select(1,8)):
            with m.Case(0x00): # Connect LED
                m.d.sync+=self.connected.eq(self.rxBlock.word_select(2,8)==C(1,8))
            with m.Case(0x01): # Running LED
                m.d.sync+=self.running.eq(self.rxBlock.word_select(2,8)==C(1,8))
            with m.Default():
                self.RESP_Invalid(m)
    # -------------------------------------------------------------------------------------
    # -------------------------------------------------------------------------------------
    def RESP_Connect_Setup(self, m):
        # <b:0x02> <b:Port>
        # Perform connect operation
        self.RESP_Invalid(m)

        if (DAP_CAPABILITIES&(1<<0)):
            # SWD mode is permitted
            with m.If ((((self.rxBlock.word_select(1,8))==0) & (DAP_CONNECT_DEFAULT==1)) |
                       ((self.rxBlock.word_select(1,8))==1)):
                m.d.sync += [
                    self.txBlock.word_select(0,16).eq(Cat(self.rxBlock.word_select(0,8),C(1,8))),
                    self.dbgif.command.eq(CMD_SET_SWD),
                    self.txLen.eq(2),
                    self.dbgif.go.eq(1)
                    ]
                m.next = 'DAP_Wait_Connect_Done'

        if (DAP_CAPABILITIES&(1<<1)):
            with m.If ((((self.rxBlock.word_select(1,8))==0) & (DAP_CONNECT_DEFAULT==2)) |
                       ((self.rxBlock.word_select(1,8))==2)):
                m.d.sync += [
                    self.txBlock.word_select(0,16).eq(Cat(self.rxBlock.word_select(0,8),C(2,8))),
                    self.dbgif.command.eq(CMD_SET_JTAG),
                    self.txLen.eq(2),
                    self.dbgif.go.eq(1)
                    ]
                m.next = 'DAP_Wait_Connect_Done'

    def RESP_Wait_Connect_Done(self, m):
        # Generic wait for inferior to process command
        with m.If((self.dbgif.go==1) & (self.dbg_done==0)):
            m.d.sync+=self.dbgif.go.eq(0)
        with m.If((self.dbgif.go==0) & (self.dbg_done==1)):
            m.next='RESPOND'
    # -------------------------------------------------------------------------------------
    # -------------------------------------------------------------------------------------
    def RESP_Wait_Done(self, m):
        # Generic wait for inferior to process command
        with m.If((self.dbgif.go==1) & (self.dbg_done==0)):
            m.d.sync+=self.dbgif.go.eq(0)
        with m.If((self.dbgif.go==0) & (self.dbg_done==1)):
            m.d.sync += self.txBlock.bit_select(8,8).eq(Mux(self.dbgif.perr,0xff,0))
            m.next='RESPOND'
    # -------------------------------------------------------------------------------------
    def RESP_Disconnect(self, m):
        # <b:0x03>
        # Perform disconnect
        m.d.sync += [
            self.running.eq(0),
            self.connected.eq(0)
        ]
        m.next = 'RESPOND'
    # -------------------------------------------------------------------------------------
    def RESP_WriteABORT(self, m):
        # <b:0x08> <b:DapIndex> <w:AbortCode>
        # Post abort code to register
        # TODO: Add ABORT for JTAG
        m.d.sync += [
            self.dbgif.command.eq(CMD_TRANSACT),
            self.dbgif.apndp.eq(0),
            self.dbgif.rnw.eq(0),
            self.dbgif.addr32.eq(0),
            self.dbgif.dwrite.eq(self.rxBlock.bit_select(16,32)),
            self.dbgif.go.eq(1)
        ]

        m.next = 'DAP_Wait_Done'
    # -------------------------------------------------------------------------------------
    def RESP_Delay(self, m):
        # <b:0x09> <s:Delay>
        # Delay for programmed number of uS
        m.d.sync += [
            self.dbgif.dwrite.eq( Cat(self.rxBlock.bit_select(16,8),self.rxBlock.bit_select(8,8))),
            self.dbgif.command.eq( CMD_WAIT ),
            self.dbgif.go.eq(1)
        ]
        m.next = 'DAP_Wait_Done'
    # -------------------------------------------------------------------------------------
    def RESP_ResetTarget(self, m):
        # <b:0x0A>
        # Reset the target
        m.d.sync += [
            self.txBlock.bit_select(8,16).eq(Cat(C(0,8),C(1,1),C(0,7))),
            self.txLen.eq(3),
            self.dbgif.command.eq( CMD_RESET ),
            self.dbgif.go.eq(1)
        ]
        m.next = 'DAP_Wait_Done'
    # -------------------------------------------------------------------------------------
    def RESP_SWJ_Pins_Setup(self, m):
        # <b:0x10> <b:PinOutput> <b:PinSelect> <w:PinWait>
        # Control and monitor SWJ/JTAG pins
        m.d.sync += [
            self.dbgif.pinsin.eq( self.rxBlock.bit_select(8,16) ),
            self.dbgif.countdown.eq( self.txBlock.bit_select(24,32) )
            ]
        m.next = 'DAP_SWJ_Pins_PROCESS';

    def RESP_SWJ_Pins_Process(self, m):
        # Spin waiting for debug interface to do its thing
        with m.If (self.dbg_done):
            m.d.sync += [
                self.txBlock.word_select(1,8).eq(self.dbgif.pinsout),
                self.txLen.eq(2)
            ]
        m.next = 'RESPOND'
    # -------------------------------------------------------------------------------------
    def RESP_SWJ_Clock(self, m):
        # <0x11> <w:newclock>
        # Set clock frequency for JTAG and SWD comms
        m.d.sync += [
            self.dbgif.dwrite.eq( self.rxBlock.bit_select(8,32) ),
            self.dbgif.command.eq( CMD_SET_CLK ),
            self.dbgif.go.eq(1)
            ]
        m.next = 'DAP_Wait_Done'
    # -------------------------------------------------------------------------------------
    # -------------------------------------------------------------------------------------
    def RESP_SWJ_Sequence_Setup(self, m):
        # <b:0x12> <b:Count> [n x <bSeqDat>.....]
        # Generate SWJ Sequence data
        m.d.sync += [
            # Number of bits to be transferred
            self.transferCount.eq(Mux(self.rxBlock.bit_select(8,8),Cat(self.rxBlock.bit_select(8,8),C(0,8)),C(256,16))),
            self.txb.eq(0),

            # Setup to have control over swdo, swclk and swwr (set for output), with clocks of 1 clock cycle
            self.dbgif.dwrite.eq(0),
            self.dbgif.pinsin.eq(0b0001_0011_0001_0000),
            self.bitcount.eq(0),
            self.dbgif.command.eq(CMD_PINS_WRITE)
            ]
        m.next = 'DAP_SWJ_Sequence_PROCESS'

    def RESP_SWJ_Sequence_Process(self, m):
        with m.Switch(self.txb):
            with m.Case(0): # Grab next octet(s) from stream ------------------------------------------------------------
                with m.If(self.streamOut.valid & self.streamOut.ready):
                    m.d.sync += [
                        self.tfrData.eq(self.streamOut.payload),
                        self.txb.eq(1),
                        self.busy.eq(1)
                    ]
                with m.Else():
                    m.d.sync += self.busy.eq(0)

            with m.Case(1): # Write the data bit -----------------------------------------------------------------------
                m.d.sync += [
                    self.dbgif.pinsin[0:2].eq(Cat(C(0,1),self.tfrData.bit_select(0,1))),
                    self.tfrData.eq(Cat(C(1,0),self.tfrData[1:8])),
                    self.transferCount.eq(self.transferCount-1),
                    self.dbgif.go.eq(1),
                    self.bitcount.eq(self.bitcount+1),
                    self.txb.eq(2)
                ]

            with m.Case(2): # Wait for bit to be accepted, then we can drop clk ----------------------------------------
                with m.If(self.dbg_done==0):
                    m.d.sync += self.dbgif.go.eq(0)
                with m.If ((self.dbgif.go==0) & (self.dbg_done==1)):
                    m.d.sync += [
                        self.dbgif.pinsin[0].eq(1),
                        self.dbgif.go.eq(1),
                        self.txb.eq(3)
                        ]

            with m.Case(3): # Now wait for clock to be complete, and move to next bit ----------------------------------
                with m.If(self.dbg_done==0):
                    m.d.sync += self.dbgif.go.eq(0)
                with m.If ((self.dbgif.go==0) & (self.dbg_done==1)):
                    with m.If(self.transferCount!=0):
                        m.d.sync += self.txb.eq(Mux(self.bitcount,1,0))
                    with m.Else():
                        m.next = 'DAP_Wait_Done'

    # -------------------------------------------------------------------------------------
    # -------------------------------------------------------------------------------------
    def RESP_SWD_Configure(self, m):
        # <0x13> <ConfigByte>
        # Setup configuration for SWD
        m.d.sync += [
            self.dbgif.dwrite.eq( self.rxBlock.bit_select(8,8) ),
            self.dbgif.command.eq( CMD_SET_SWD_CFG ),
            self.dbgif.go.eq(1)
            ]
        m.next = 'DAP_Wait_Done'
    # -------------------------------------------------------------------------------------
    # -------------------------------------------------------------------------------------
    def RESP_JTAG_Configure(self, m):
        # <b:0x15> <b:Count> n x [ <b:IRLength> ]
        # Set IR Length for Chain

        m.d.sync += [
            # We cope with up to 5 devices with IRLen of 1..32 bits
            self.dbgif.dwrite.eq( Cat( self.rxBlock.bit_select(11,5)-1,
                                       self.rxBlock.bit_select(19,5)-1,
                                       self.rxBlock.bit_select(27,5)-1,
                                       self.rxBlock.bit_select(35,5)-1,
                                       self.rxBlock.bit_select(43,5)-1,
                                       self.rxBlock.bit_select(51,5)-1,
                                       C(2,0) )
                                 ),
            self.dbgif.command.eq( CMD_SET_JTAG_CFG ),
            self.dbgif.go.eq(1)
            ]
        m.next = 'DAP_Wait_Done'
    # -------------------------------------------------------------------------------------
    # -------------------------------------------------------------------------------------
    def RESP_JTAG_IDCODE_Setup(self, m):
        # <b:0x16> <b:JTAGIndex>
        # Request ID code for specified device
        m.d.sync += [
            self.dbgif.command.eq(CMD_JTAG_GET_ID),
            self.dbgif.dwrite.eq( self.rxBlock.bit_select(8,8) ),
            self.txLen.eq(6),
            self.txBlock.bit_select(16,32).eq(0),
            self.dbgif.go.eq(1)
            ]

        m.next = 'JTAG_IDCODE_Process'

    def RESP_JTAG_IDCODE_Process(self, m):
        with m.If(self.dbg_done==0):
            m.d.sync += self.dbgif.go.eq(0)
        with m.Elif(self.dbg_done==1):
            m.d.sync += self.txBlock.bit_select(16,32).eq(self.dbgif.dread)
            m.next = "RESPOND"

    # -------------------------------------------------------------------------------------
    def RESP_TransferConfigure(self, m):
        # <b:0x04> <b:IdleCycles> <s:WaitRetry> <s:MatchRetry>
        # Configure transfer parameters
        m.d.sync += [
            self.waitRetry.eq(self.rxBlock.bit_select(16,16)),
            self.matchRetry.eq(self.rxBlock.bit_select(32,16)),

            # Send idleCycles to layers below
            self.dbgif.dwrite.eq(self.rxBlock.bit_select(8,8)),
            self.dbgif.command.eq(CMD_SET_TFR_CFG),
            self.dbgif.go.eq(1)
        ]
        m.next = 'DAP_Wait_Done'
    # -------------------------------------------------------------------------------------
    # -------------------------------------------------------------------------------------
    def RESP_Transfer_Setup(self, m):
        # <0x05> <b:DapIndex> <b:TfrCount] n x [ <b:TfrReq> <w:TfrData>]
        # Triggered at start of a Transfer data sequence
        # We have the command, index and transfer count, need to set up to get the transfers

        m.d.sync += [
            self.dapIndex.eq(self.rxBlock.bit_select(8,8)),
            self.transferCount.eq(self.rxBlock.bit_select(16,8)),
            self.tfrram.adr.eq(0),
            self.busy.eq(1),
            self.txb.eq(0)
        ]

        # Filter for case someone tries to send us no transfers to perform
        # in which case we send back a good ack!
        with m.If(self.rxBlock.bit_select(16,8)!=0):
            m.next = 'DAP_Transfer_PROCESS'
        with m.Else():
            m.d.sync += [
                self.txBlock.word_select(2,8).eq(C(1,8)),
                self.busy.eq(0),
                self.txLen.eq(3)
                ]
            m.next = 'RESPOND'


    def RESP_Transfer_Process(self, m):
        m.d.comb += self.tfrram.dat_w.eq(self.dbgif.dread)

        # By default we don't want to receive any more stream data
        m.d.sync += self.busy.eq(1)

        with m.Switch(self.txb):
            with m.Case(0): # Get transfer request from stream, or the previous one if the post is finishing ----------
                with m.If(~(self.streamOut.valid & self.streamOut.ready)):
                    m.d.sync += self.busy.eq(0)
                with m.Else():
                    m.d.sync += [
                        self.tfrReq.eq(self.streamOut.payload),
                        self.retries.eq(0)
                    ]

                    # This is a good transaction from the stream, so record the fact it's in flow
                    m.d.sync += self.txBlock.word_select(1,8).eq(self.txBlock.word_select(1,8)+1)

                    # So now go do the read or write as appropriate
                    with m.If ((~self.streamOut.payload.bit_select(1,1)) |
                               self.streamOut.payload.bit_select(4,1) |
                               self.streamOut.payload.bit_select(5,1) ):

                        # Need to collect the value
                        m.d.sync += self.txb.eq(1)
                    with m.Else():
                        # It's a read, no value to collect
                        m.d.sync += [
                            self.txb.eq(5),
                            self.busy.eq(1)
                        ]

            with m.Case(1,2,3,4): # Collect the 32 bit transfer Data to go with the command ----------------------------
                with m.If(self.streamOut.valid & self.streamOut.ready):
                    m.d.sync+=[
                        self.tfrData.word_select(self.txb-1,8).eq(self.streamOut.payload),
                        self.txb.eq(self.txb+1)
                    ]

                    with m.If(self.tfrReq.bit_select(5,1) & (self.txb==5)):
                        # This is a match register write
                        m.d.sync += [
                            self.mask.eq(Cat(self.streamOut.payload,self.tfrData.bit_select(0,24))),
                            self.txb.eq(0)
                        ]
                with m.Else():
                    m.d.sync +=self.busy.eq(0)

            with m.Case(5): # We have the command and any needed data, action it ---------------------------------------
                m.d.sync += [
                    self.dbgif.command.eq(CMD_TRANSACT),
                    self.dbgif.apndp.eq(self.tfrReq.bit_select(0,1)),
                    self.dbgif.rnw.eq(self.tfrReq.bit_select(1,1)),
                    self.dbgif.addr32.eq(self.tfrReq.bit_select(2,2)),
                    self.dbgif.dwrite.eq(self.tfrData),
                    self.dbgif.go.eq(1),
                    self.txb.eq(self.txb+1),
                ]

            with m.Case(6): # We sent a command, wait for it to start being executed -----------------------------------
                with m.If(self.dbg_done==0):
                    m.d.sync+=[
                        self.dbgif.go.eq(0),
                        self.txb.eq(7)
                    ]

            with m.Case(7): # Wait for command to complete -------------------------------------------------------------
                with m.If(self.dbg_done==1):
                    # Write return value from this command into return frame
                    m.d.sync += self.txBlock.word_select(2,8).eq(Cat(self.dbgif.ack,self.dbgif.perr)),

                    # Now lets figure out how to handle this response....

                    # If we're to retry, then lets do it
                    with m.If(self.dbgif.ack==0b010):
                        m.d.sync += [
                            self.retries.eq(self.retries+1),
                            self.txb.eq(Mux((self.retries<self.waitRetry),5,8))
                        ]

                    with m.Elif(self.tfrReq.bit_select(4,1)):
                        # This is a transfer match request
                        with m.If(((self.dbgif.dread & self.mask) !=self.tfrData) & (self.matchretries<self.matchRetry)):
                            # Not a match and we've run out of attempts, so set bit 4
                            m.d.sync += self.txBlock.bit_select(21,1).eq(1)
                            m.d.sync += self.txb.eq(8)
                        with m.Else():
                            m.d.sync += self.txb.eq(5)

                    with m.Else():
                        # Check to see if this is a new post (i.e. data to be ignored), or data
                        with m.If(self.dbgif.again | ((~self.dbgif.ignoreData) & self.dbgif.rnw)):
                            # We're instructed to write this
                            m.d.sync += self.tfrram.adr.eq(self.tfrram.adr+1)

                        # It it was a good transfer, then keep going if appropriate
                        with m.If(self.dbgif.again):
                            # Just repeat this send
                            m.d.sync += self.txb.eq(5)
                        with m.Else():
                            # This transaction is something we want to record
                            m.d.sync += self.transferCount.eq(self.transferCount-1)

                            with m.If((self.dbgif.ack==1) & (self.dbgif.perr==0) & (self.transferCount>1)):
                                m.d.sync += self.txb.eq(0)
                            with m.Else():
                                with m.If(self.dbgif.postedMode):
                                    # Debug interface is in posting mode, better do one final read to collect the data
                                    m.d.sync += [
                                        self.tfrReq.eq(0x0E), # Read RDBUFF
                                        self.retries.eq(0),
                                        self.txb.eq(5)
                                    ]
                                with m.Else():
                                    # Otherwise let's wrap up
                                    # All data have been processed, now lets send them back
                                    m.d.sync += self.txb.eq(8)

            with m.Case(8,9,10): # Transfer completed, start sending data back -----------------------------------------
                with m.If(self.streamIn.ready):
                    m.d.sync += [
                        self.streamIn.payload.eq(self.txBlock.word_select(self.txb-8,8)),
                        self.streamIn.valid.eq(1),
                        self.txb.eq(self.txb+1),
                        self.streamIn.last.eq(self.isV2 & (self.txb==10) & (self.tfrram.adr==0))
                    ]

            with m.Case(11): # Initial data sent, send any remaining material ------------------------------------------
                m.next = 'UPLOAD_RXED_DATA'
                m.d.sync += [
                    self.txb.eq(0),
                    self.txedLen.eq((self.tfrram.adr*4)+3)  # Record length of data to be returned
                ]

    # -------------------------------------------------------------------------------------
    # -------------------------------------------------------------------------------------
    def RESP_TransferBlock_Setup(self, m):
        # <B:0x06> <B:DapIndex> <S:TransferCount> <B:TransferReq> n x [ <W:TransferData> ])
        # Triggered at start of a TransferBlock data sequence
        # We have the command, index and transfer count, need to set up to get the transfers

        m.d.sync += [
            self.tfrram.adr.eq(0),
            self.dbgif.command.eq(CMD_TRANSACT),
            self.retries.eq(0),

            # DAP Index is 1 byte in
            self.dapIndex.eq(self.rxBlock.bit_select(8,8)),

            # Transfer count is 2 bytes in
            self.transferCount.eq(self.rxBlock.bit_select(16,16)),

            # Transfer Req is 4 bytes in
            self.dbgif.apndp.eq(self.rxBlock.bit_select(32,1)),
            self.dbgif.rnw.eq(self.rxBlock.bit_select(33,1)),
            self.dbgif.addr32.eq(self.rxBlock.bit_select(34,2)),

            # Set to one the number of responses sent back
            self.txBlock.bit_select(8,16).eq(C(1,16)),

            # Decide which state to jump to depending on if we have data
            self.txb.eq(Mux(self.rxBlock.bit_select(33,1),4,0)),

            # ...and start the retries counter for this first entry
            self.retries.eq(0)
        ]

        # Filter for case someone tries to send us no transfers to perform
        # in which case we send back a good ack!
        with m.If(self.rxBlock.bit_select(16,16)):
            m.next = 'DAP_TransferBlock_PROCESS'
        with m.Else():
            m.d.sync += [
                self.txBlock.bit_select(8,24).eq(C(1,24)),
                self.txLen.eq(4)
            ]
            m.next = 'RESPOND'

    def RESP_TransferBlock_Process(self, m):
        m.d.comb += self.tfrram.dat_w.eq(self.dbgif.dread)

        # By default we don't want to receive any more stream data, we're not writing to the ram
        # and it's not the end of a packet
        m.d.sync += self.busy.eq(1)

        with m.Switch(self.txb):
            with m.Case(0,1,2,3): # Collect the 32 bit transfer Data to go with the command ----------------------------
                with m.If(self.streamOut.ready & self.streamOut.valid):
                    m.d.sync+=[
                        self.tfrData.word_select(self.txb,8).eq(self.streamOut.payload),
                        self.txb.eq(self.txb+1),
                    ]
                with m.Else():
                    m.d.sync +=self.busy.eq(0)

            with m.Case(4): # We have the command and any needed data, action it ---------------------------------------
                m.d.sync += [
                    self.dbgif.dwrite.eq(self.tfrData),
                    self.dbgif.go.eq(1),
                    self.retries.eq(self.retries+1),
                    self.txb.eq(5)
                ]

            with m.Case(5): # Wait for command to be accepted ----------------------------------------------------------
                with m.If(self.dbg_done==0):
                    m.d.sync += self.dbgif.go.eq(0)
                    m.d.sync += self.txb.eq(6)

            with m.Case(6): # We sent a command, wait for it to start being executed -----------------------------------
                with m.If(self.dbg_done==1):
                    # Write return value from this command into return frame
                    m.d.sync += self.txBlock.bit_select(24,8).eq(Cat(self.dbgif.ack, self.dbgif.perr))

                    # Now lets figure out how to handle this response

                    # If we're to retry, then let's do it
                    with m.If(self.dbgif.ack==0b010):
                        m.d.sync += self.txb.eq(Mux((self.retries<self.waitRetry),4,7))

                    with m.Else():
                        with m.If((~self.dbgif.ignoreData) & self.dbgif.rnw):
                            # If this is something that resulted in data, then store the data
                            m.d.sync += [
                                self.tfrram.adr.eq(self.tfrram.adr+1),
                            ]

                        with m.If(self.dbgif.again):
                            # We need to repeat this request with the same parameters
                            m.d.sync += [
                                self.retries.eq(0),
                                self.dbgif.go.eq(1),
                                self.txb.eq(5)
                            ]

                        with m.Else():
                            # Keep going if appropriate
                            m.d.sync += self.transferCount.eq(self.transferCount-1)
                            with m.If((self.dbgif.ack==1) & (self.dbgif.perr==0) & (self.transferCount>1)):
                                m.d.sync += [
                                    self.retries.eq(0),
                                    self.txBlock.bit_select(8,16).eq(self.txBlock.bit_select(8,16)+1),
                                    self.txb.eq(Mux(self.dbgif.rnw,4,0))
                                ]

                            with m.Else():
                                with m.If(self.dbgif.postedMode):
                                    # Debug interface is in posting mode, better do one more read to collect the data
                                    m.d.sync += [
                                        self.dbgif.rnw.eq(1),     # Read RDBUFF
                                        self.retries.eq(0),
                                        self.dbgif.apndp.eq(0),
                                        self.dbgif.addr32.eq(3),
                                        self.txb.eq(4)
                                    ]
                                with m.Else():
                                    # Otherwise lets wrap up
                                    m.d.sync += [
                                        # Only need to increment transfer count ram position if this was a read
                                        #self.transferCount.eq(self.tfrram.adr+self.dbgif.rnw),
                                        #self.tfrram.adr.eq(0),
                                        self.txb.eq(7)
                                    ]

            with m.Case(7,8,9,10): # Transfer completed, start sending data back ---------------------------------------
                with m.If(self.streamIn.ready):
                    m.d.sync += [
                        self.streamIn.payload.eq(self.txBlock.word_select(self.txb-7,8)),
                        self.streamIn.valid.eq(1),
                        self.txb.eq(self.txb+1),
                        # End of transfer if there are no data to return
                        self.streamIn.last.eq(self.isV2 & (self.txb==10) & (self.dbgif.rnw==0))
                    ]

            with m.Case(11): # Initial data sent, decide what to do next ----------------------------------------------
                m.d.sync += [
                    self.txb.eq(0),
                    self.txedLen.eq((self.tfrram.adr*4)+4)  # Record length of data that will be returned
                ]
                m.next = 'UPLOAD_RXED_DATA'

    def RESP_Transfer_Complete(self, m):
        # Complete the process of returning data collected via either Transfer_Process or
        # TransferBlock_Process. Data count to be transferred is in self.transferCount and
        # the payload is in the tfrram.

        m.d.sync += self.busy.eq(1)

        with m.Switch(self.txb):
            with m.Case(0): # Prepare transfer ------------------------------------------------------------------------
                with m.If(self.tfrram.adr!=0):
                    m.d.sync += [
                        self.transferCount.eq(self.tfrram.adr),
                        self.tfrram.adr.eq(0),
                        self.txb.eq(1)
                        ]
                with m.Else():
                    m.d.sync += self.txb.eq(7)

            with m.Case(1): # Wait for ram to propagate through -------------------------------------------------------
                m.d.sync += self.txb.eq(2)

            with m.Case(2): # Collect transfer value from RAM store ---------------------------------------------------
                m.d.sync += [
                    self.transferCount.eq(self.transferCount-1),
                    self.streamIn.payload.eq(self.tfrram.dat_r.word_select(0,8)),
                    self.txb.eq(3)
                ]

            with m.Case(3,4,5,6): # Send 32 bit value to outgoing stream -------------------------------------------
                m.d.sync += self.streamIn.valid.eq(1)
                with m.If(self.streamIn.ready & self.streamIn.valid):
                    m.d.sync += [
                        self.txb.eq(self.txb+1),
                        self.streamIn.payload.eq(self.tfrram.dat_r.word_select(self.txb-2,8)),
                        # 5 because of pipeline
                        self.streamIn.last.eq(self.isV2 & (self.transferCount==0) & (self.txb==5)),
                        self.streamIn.valid.eq(self.txb!=6)
                    ]

            with m.Case(7): # Finished this send ---------------------------------------------------------------------
                with m.If(self.streamIn.ready):
                    with m.If(self.transferCount==0):
                        with (m.If(self.isV2)):# | (self.txedLen==DAP_V1_MAX_PACKET_SIZE))):
                            m.next = 'IDLE'
                        with m.Else():
                            m.next = 'V1PACKETFILL'
                    with m.Else():
                        m.d.sync += [
                            self.txb.eq(1),
                            self.tfrram.adr.eq(self.tfrram.adr+1)
                        ]

    # -------------------------------------------------------------------------------------
    # -------------------------------------------------------------------------------------
    def RESP_JTAG_Sequence_Setup(self,m):
        # Triggered at the start of a RESP JTAG Sequence
        # There are data to receive at this point, and potentially bytes to transmit

        # Collect how many sequences we'll be processing, then move to get the first one
        m.d.sync += [
            self.seqCount.eq(self.rxBlock.word_select(1,8)),

            # Setup to have control over tms, tdi and swwr (set for output), with clocks of 1 clock cycle
            self.dbgif.dwrite.eq(0),

            # In case this is CMSIS-DAP v1, keep a tally of whats been sent so we can pad the packet
            self.txedLen.eq(2),

            # Just for now take over reset as well
            self.dbgif.pinsin.eq(0b0001_0111_0001_0000),
            self.dbgif.command.eq(CMD_PINS_WRITE),
            self.txb.eq(0)
        ]
        m.next = 'DAP_JTAG_Sequence_PROCESS'


    def RESP_JTAG_Sequence_PROCESS(self,m):
        m.d.sync += [
            self.busy.eq(1),
            self.streamIn.valid.eq(0)
            ]


        m.d.sync += self.can.eq(0)

        with m.Switch(self.txb):

            # -------------- # Send frontmatter
            with m.Case(0):
                with m.If(self.streamIn.ready):
                    m.d.sync += [
                        # Send frontmatter for reponse
                        self.streamIn.payload.eq(DAP_JTAG_Sequence),
                        self.streamIn.last.eq(0),
                        self.streamIn.valid.eq(1),

                        # This is the 'OK' that will be sent out next
                        self.pendingTx.eq(0),

                        # If there's nothing to be done then we are finished, otherwise start
                        self.txb.eq(Mux(self.seqCount!=0,1,7))
                    ]

            # --------------
            with m.Case(1): # Get info for this sequence
                with m.If(self.streamOut.ready & self.streamOut.valid):
                    m.d.sync += [
                        self.seqCount.eq(self.seqCount-1),
                        self.tckCycles.eq(self.streamOut.payload.bit_select(0,6)),

                        # Set the TMS bit
                        self.dbgif.pinsin.bit_select(1,1).eq(self.streamOut.payload.bit_select(6,1)),

                        # ...and decide if we want to capture what comes back
                        self.tdotgt.eq(Mux(self.streamOut.payload.bit_select(7,1),
                                           Mux(self.streamOut.payload.bit_select(0,6),self.streamOut.payload.bit_select(0,6),0x40),0)),

                        self.txb.eq(2)
                    ]
                with m.Else():
                    m.d.sync += self.busy.eq(0)

            # --------------
            with m.Case(2): # Waiting for TDI byte to arrive
                with m.If(self.streamOut.ready & self.streamOut.valid):
                    m.d.sync += [
                        self.tdiData.eq(self.streamOut.payload),
                        self.tdiCount.eq(0),

                        self.txb.eq(3)
                    ]
                with m.Else():
                    m.d.sync += self.busy.eq(0)

            # --------------
            with m.Case(3): # Setup for clocking out TDI, TCK->0
                m.d.sync += [
                    # Put this bit ready to output
                    self.dbgif.pinsin.bit_select(2,1).eq(self.tdiData.bit_select(self.tdiCount,1)),
                    self.dbgif.pinsin.bit_select(0,1).eq(0),
                    self.dbgif.go.eq(1),

                    self.txb.eq(4)
                ]

            # -------------
            with m.Case(4): # Waiting until we can set TCK->1
                with m.If(self.dbg_done==0):
                    m.d.sync += self.dbgif.go.eq(0)
                with m.If ((self.dbgif.go==0) & (self.dbg_done==1)):
                    m.d.sync += [
                        # Bit is established, change the clock
                        self.dbgif.pinsin.bit_select(0,1).eq(1),
                        self.dbgif.go.eq(1),

                        self.txb.eq(5)
                    ]

            # -------------
            with m.Case(5): # Sent this bit, waiting for clock 1 to complete
                with m.If(self.dbg_done==0):
                    m.d.sync += self.dbgif.go.eq(0)
                with m.If ((self.dbgif.go==0) & (self.dbg_done==1)):
                    m.d.sync += [
                        # Adjust all the pointers
                        self.tckCycles.eq(self.tckCycles-1),
                        self.tdiCount.eq(self.tdiCount+1),

                        self.txb.eq(6)
                    ]

                    # If there is a capture in process then do it
                    with m.If(self.tdotgt):
                        m.d.sync += [
                            self.can.eq(self.dbgif.pinsout.bit_select(3,1)),
                            self.tdoBuild.bit_select(self.tdoCount,1).eq(self.dbgif.pinsout.bit_select(3,1)),
                            self.tdoCount.eq(self.tdoCount+1),
                            self.tdotgt.eq(self.tdotgt-1)
                        ]

            # -------------
            with m.Case(6): # ...if this capture is complete then send it back, then decide if there is still work to be done
                with m.If(((self.tdotgt==0) & (self.tdoCount!=0)) | (self.tdoCount==8)):
                    with m.If(self.streamIn.ready):
                        m.d.sync += [
                            self.streamIn.payload.eq(self.pendingTx),
                            self.streamIn.valid.eq(1),
                            self.pendingTx.eq(self.tdoBuild),
                            self.tdoCount.eq(0),
                            self.tdoBuild.eq(0),
                            self.txedLen.eq(self.txedLen+1)
                        ]
                with m.Else():
                    with m.If(self.tckCycles==0):
                        # This is the last bit of the sequence, go get the next, or finish
                        m.d.sync += self.txb.eq(Mux(self.seqCount,1,7))
                    with m.Else():
                        # otherwise set up the next bit to clock out, or a new byte
                        m.d.sync += self.txb.eq(Mux(self.tdiCount==8,2,3))

            # -------------
            with m.Case(7): # Send the final byte, with last set
                with m.If(self.streamIn.ready):
                    m.d.sync += [
                        self.streamIn.payload.eq(self.pendingTx),
                        self.streamIn.last.eq(self.isV2 | (self.txedLen==64)),
                        self.streamIn.valid.eq(1),
                        self.txb.eq(8)
                    ]

            # -------------
            with m.Case(8): # Now decide how to terminate
                    with m.If(self.isV2 | (self.txedLen==64)):
                        m.next = 'IDLE'
                    with m.Else():
                        m.next = 'V1PACKETFILL'

    # -------------------------------------------------------------------------------------

    def elaborate(self,platform):
        done_cdc      = Signal(2)
        self.dbg_done = Signal()

        m = Module()
        # Reset everything before we start

        m.d.sync += self.streamIn.valid.eq(0)
        m.d.comb += self.streamOut.ready.eq(~self.busy)

        m.submodules.tfrram = self.tfrram = WideRam()

        m.submodules.dbgif = self.dbgif = DBGIF(self.dbgpins)

        # Organise the CDC from the debug interface
        m.d.sync += done_cdc.eq(Cat(done_cdc[1],self.dbgif.done))
        m.d.comb += self.dbg_done.eq(done_cdc==0b11)

        # Latch the read data at the rising edge of done signal
        m.d.comb += self.tfrram.we.eq(done_cdc==0b10)

        with m.FSM(domain="sync") as decoder:
            with m.State('IDLE'):
                m.d.sync += [ self.txedLen.eq(0), self.busy.eq(0)  ]

                # Only process if this is the start of a packet (i.e. it's not overrrun or similar)
                with m.If(self.streamOut.valid & self.streamOut.ready & self.streamOut.first):
                    m.next = 'ProtocolError'
                    m.d.sync += self.rxedLen.eq(1)
                    m.d.sync += self.rxBlock.word_select(0,8).eq(self.streamOut.payload)

                    # Default return is packet name followed by 0 (no error)
                    m.d.sync += self.txBlock.word_select(0,16).eq(Cat(self.streamOut.payload,C(0,8)))
                    m.d.sync += self.txLen.eq(2)

                    with m.Switch(self.streamOut.payload):
                        with m.Case(DAP_Disconnect, DAP_ResetTarget, DAP_SWO_Status, DAP_TransferAbort):
                            m.d.sync+= [ self.rxLen.eq(1), self.busy.eq(1) ]
                            # This still goes to RxParams as a common entry, but then it dispatches immediately
                            # from there as there are no params to rx
                            m.next='RxParams'

                        with m.Case(DAP_Info, DAP_Connect, DAP_SWD_Configure, DAP_SWO_Transport, DAP_SWJ_Sequence,
                                    DAP_SWO_Mode, DAP_SWO_Control, DAP_SWO_ExtendedStatus, DAP_JTAG_IDCODE, DAP_JTAG_Sequence):
                            m.d.sync+=self.rxLen.eq(2)
                            with m.If(~self.streamOut.last):
                                m.next = 'RxParams'

                        with m.Case(DAP_HostStatus, DAP_SWO_Data, DAP_Delay, DAP_JTAG_Configure, DAP_Transfer):
                            m.d.sync+=self.rxLen.eq(3)
                            with m.If(~self.streamOut.last):
                                m.next = 'RxParams'

                        with m.Case(DAP_SWO_Baudrate, DAP_SWJ_Clock, DAP_TransferBlock):
                            m.d.sync+=self.rxLen.eq(5)
                            with m.If(~self.streamOut.last):
                                m.next = 'RxParams'

                        with m.Case(DAP_WriteABORT, DAP_TransferConfigure):
                            m.d.sync+=self.rxLen.eq(6)
                            with m.If(~self.streamOut.last):
                                m.next = 'RxParams'

                        with m.Case(DAP_SWJ_Pins):
                            m.d.sync+=self.rxLen.eq(7)
                            with m.If(~self.streamOut.last):
                                m.next = 'RxParams'

                        with m.Case(DAP_SWD_Sequence):
                            with m.If(~self.streamOut.last):
                                m.next = 'DAP_SWD_Sequence_GetCount'

                        with m.Case(DAP_ExecuteCommands):
                            with m.If(~self.streamOut.last):
                                m.next = 'DAP_ExecuteCommands_GetNum'

                        with m.Case(DAP_QueueCommands):

                            with m.If(~self.streamOut.last):
                                m.next = 'DAP_QueueCommands_GetNum'

                        with m.Default():
                            self.RESP_Invalid(m)

    #########################################################################################

            with m.State('RESPOND'):
                with m.If(self.txedLen<self.txLen):
                    m.d.sync += [
                        self.streamIn.valid.eq(1),
                        self.streamIn.payload.eq(self.txBlock.word_select(self.txedLen,8)),

                        # This is the end of the packet if we've filled the length and it's v2
                        # or if we've filled the packet and it's v1
                        self.streamIn.last.eq(self.isV2 & (self.txedLen==self.txLen-1)),
                    ]

                    with m.If(self.streamIn.ready & self.streamIn.valid):
                        m.d.sync += [
                            self.txedLen.eq(self.txedLen+1),
                            self.streamIn.valid.eq(0)
                        ]

                with m.Elif(self.isV2 | (self.txedLen==DAP_V1_MAX_PACKET_SIZE)):
                    # Everything is transmitted, return to idle condition
                    m.d.sync += [
                        self.streamIn.valid.eq(0),
                        self.busy.eq(0)
                    ]
                    m.next = 'IDLE'
                with m.Else():
                    m.next = 'V1PACKETFILL'


            with m.State('V1PACKETFILL'):
                with m.If(self.txedLen<DAP_V1_MAX_PACKET_SIZE):
                    m.d.sync += [
                        self.streamIn.valid.eq(1),
                        self.streamIn.payload.eq(0),
                    ]

                    with m.If(self.txedLen<DAP_V1_MAX_PACKET_SIZE-1):
                        with m.If(self.streamIn.ready & self.streamIn.valid):
                            m.d.sync += [
                                self.txedLen.eq(self.txedLen+1),
                                self.streamIn.valid.eq(0)
                            ]
                    with m.Else():
                        self.streamIn.valid.eq(0),
                        self.busy.eq(0)
                        m.next = 'IDLE'

                with m.Else():
                    self.streamIn.valid.eq(0),
                    self.busy.eq(0)
                    m.next = 'IDLE'

    #########################################################################################

            with m.State('RxParams'):
                # ---- Action dispatcher --------------------------------------
                # If we've got everything for this packet then let's process it
                with m.If(self.rxedLen==self.rxLen):
                    with m.Switch(self.rxBlock.word_select(0,8)):

                        # General Commands
                        # ================
                        with m.Case(DAP_Info):
                            self.RESP_Info(m)

                        with m.Case(DAP_HostStatus):
                            self.RESP_HostStatus(m)

                        with m.Case(DAP_Connect):
                            self.RESP_Connect_Setup(m)

                        with m.Case(DAP_Disconnect):
                            self.RESP_Disconnect(m)

                        with m.Case(DAP_WriteABORT):
                            self.RESP_WriteABORT(m)

                        with m.Case(DAP_Delay):
                            self.RESP_Delay(m)

                        with m.Case(DAP_ResetTarget):
                            self.RESP_ResetTarget(m)

                        # Common SWD/JTAG Commands
                        # ========================
                        with m.Case(DAP_SWJ_Pins):
                            self.RESP_SWJ_Pins_Setup(m)

                        with m.Case(DAP_SWJ_Clock):
                            self.RESP_SWJ_Clock(m)

                        with m.Case(DAP_SWJ_Sequence):
                            self.RESP_SWJ_Sequence_Setup(m)

                        # SWD Commands
                        # ============
                        with m.Case(DAP_SWD_Configure):
                            self.RESP_SWD_Configure(m)

                        # SWO Commands
                        # ============
                        with m.Case(DAP_SWO_Transport):
                            self.RESP_Not_Implemented(m)

                        with m.Case(DAP_SWO_Mode):
                            self.RESP_Not_Implemented(m)

                        with m.Case(DAP_SWO_Baudrate):
                            self.RESP_Not_Implemented(m)

                        with m.Case(DAP_SWO_Control):
                            self.RESP_Not_Implemented(m)

                        with m.Case(DAP_SWO_Status):
                            self.RESP_Not_Implemented(m)

                        with m.Case(DAP_SWO_ExtendedStatus):
                            self.RESP_Not_Implemented(m)

                        with m.Case(DAP_SWO_Data):
                            self.RESP_Not_Implemented(m)

                        # JTAG Commands
                        # =============
                        with m.Case(DAP_JTAG_Sequence):
                            self.RESP_JTAG_Sequence_Setup(m)

                        with m.Case(DAP_JTAG_Configure):
                            self.RESP_JTAG_Configure(m)

                        with m.Case(DAP_JTAG_IDCODE):
                            self.RESP_JTAG_IDCODE_Setup(m)

                        # Transfer Commands
                        # =================
                        with m.Case(DAP_TransferConfigure):
                            self.RESP_TransferConfigure(m)

                        with m.Case(DAP_Transfer):
                            self.RESP_Transfer_Setup(m)

                        with m.Case(DAP_TransferBlock):
                            self.RESP_TransferBlock_Setup(m)

                        with m.Default():
                            self.RESP_Invalid(m)

                # Grab next byte in this packet
                with m.Elif(self.streamOut.valid & self.streamOut.ready):
                    m.d.sync += [
                        self.rxBlock.word_select(self.rxedLen,8).eq(self.streamOut.payload),
                        self.rxedLen.eq(self.rxedLen+1)
                    ]
                    # Don't grab more data if we've got what we were commanded for
                    with m.If(self.rxedLen+1==self.rxLen):
                        m.d.sync += self.busy.eq(1)

                    # Check to make sure this packet isn't foreshortened
                    with m.If(self.streamOut.last):
                        with m.If(self.rxedLen+1!=self.rxLen):
                            self.RESP_Invalid(m)


    #########################################################################################

            with m.State('DAP_SWJ_Pins_PROCESS'):
              self.RESP_SWJ_Pins_Process(m)

            with m.State('DAP_SWO_Data_PROCESS'):
              self.RESP_Not_Implemented(m)

            with m.State('DAP_SWJ_Sequence_PROCESS'):
                self.RESP_SWJ_Sequence_Process(m)

            with m.State('DAP_JTAG_Sequence_PROCESS'):
              self.RESP_JTAG_Sequence_PROCESS(m)

            with m.State('DAP_Transfer_PROCESS'):
              self.RESP_Transfer_Process(m)

            with m.State('DAP_TransferBlock_PROCESS'):
              self.RESP_TransferBlock_Process(m)

            with m.State('UPLOAD_RXED_DATA'):
              self.RESP_Transfer_Complete(m)

            with m.State('JTAG_IDCODE_Process'):
              self.RESP_JTAG_IDCODE_Process(m)

            with m.State('DAP_SWD_Sequence_GetCount'):
                self.RESP_Invalid(m)

            with m.State('DAP_TransferBlock'):
                self.RESP_Invalid(m)

            with m.State('DAP_ExecuteCommands_GetNum'):
                self.RESP_Invalid(m)

            with m.State('DAP_QueueCommands_GetNum'):
                self.RESP_Invalid(m)

            with m.State('DAP_Wait_Done'):
                self.RESP_Wait_Done(m)

            with m.State('DAP_Wait_Connect_Done'):
                self.RESP_Wait_Connect_Done(m)

            with m.State('Error'):
                self.RESP_Invalid(m)

            with m.State('ProtocolError'):
                self.RESP_Invalid(m)

        return m
