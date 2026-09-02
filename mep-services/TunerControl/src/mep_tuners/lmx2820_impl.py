# SPDX-FileCopyrightText: Copyright (c) 2025 Massachusetts Institute of Technology
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# rps 10/25/2024
# alekspop 04/11/2025
# ben welchman 06/05/2025
#
# originally lmx2820.py
# code from https://forums.raspberrypi.com/viewtopic.php?t=319702
#
#
"""
testBit
setBit
clearBit
toggleBit
modifyBM
BMofNumber
parseBytes

class:
  LMX2820
  functions:
   __init__
   LMX2820InitRegs
   LMX2820calcIntNumDen
   LMX2820setREGSforNewFreq
   LMX2820WriteSingeRegister
   LMX2820StartUp
   LMX2820ChangeFreq

"""

import os
import sys
import time

SIMU = False

# set environment variable to register the FT232H board
os.environ["BLINKA_FT232H"] = "1"

#
# many dependencies to getting the device to work
#
#
# exception raised if board not plugged in
#
try:
    import board
except Exception as eobj:
    if SIMU:
        pass
    else:
        print("Exception on 'import board\n", eobj)
        print("Exiting early")
        sys.exit()
# end except

try:
    import busio
except Exception as eobj:
    if SIMU:
        pass
    else:
        print("Exception on 'import busio\n", eobj)
        print("Exiting early")
        sys.exit()
# end except

try:
    from digitalio import DigitalInOut
except Exception as eobj:
    if SIMU:
        pass
    else:
        print("Exception on 'from digitalio import DigitalInOut\n", eobj)
        print("Exiting early")
        sys.exit()
# end except


# from pyftdi.spi import SpiController

#
# ------------------------------------------------------------------------------
#
#  GLOBALS
#

spi = None
CSpin = None

#
# ------------------------------------------------------------------------------
#
# https://wiki.python.org/moin/BitManipulation
#
# testBit() returns a nonzero result, 2**offset, if the bit at 'offset' is one.


def testBit(in_val, offset):
    retval = 0
    debug_f = False

    mask = 1 << offset
    if debug_f:
        print("mask=", mask)
    # endif debug

    test_val = in_val & mask
    if debug_f:
        print("test_val=", test_val)
    # endif debug

    return test_val


# end testBit
#
# ------------------------------------------------------------------------------
#
# setBit() returns an integer with the bit at 'offset' set to 1.


def setBit(int_type, offset):
    mask = 1 << offset
    return int_type | mask


# end setBit
#
# ------------------------------------------------------------------------------
#
# clearBit() returns an integer with the bit at 'offset' cleared.


def clearBit(int_type, offset):
    mask = ~(1 << offset)
    return int_type & mask


# end clearBit
#
# ------------------------------------------------------------------------------
#
# toggleBit() returns an integer with the bit at 'offset' inverted, 0 -> 1 and 1 -> 0.


def toggleBit(int_type, offset):
    mask = 1 << offset
    return int_type ^ mask


# end toggleBit
#
# ------------------------------------------------------------------------------
#
def modifyBM(
    dst,  # destination register
    src,  # source value
    ofs,  # bit offset
    width,
):  # number of bits to use in src
    debug_f = False

    not_mask = 0xFFFFFFFF  # 32 bits

    # create bit mask
    ii = 0
    mask = 0
    while ii < width:
        mask = mask | setBit(1, ii)
        ii = ii + 1
    # end while

    if debug_f:
        print("mask=", mask)
    # endif debug

    # create shifted field

    field = src << ofs

    # shift mask

    ofs_mask = mask << ofs

    if debug_f:
        print("ofs_mask=", "{0:016b}".format(ofs_mask))
    # endif debug

    # invert mask (can't use ~ because this does 2's complement

    ofs_mask = ofs_mask ^ not_mask

    if debug_f:
        print("ofs_mask=", "{0:016b}".format(ofs_mask))
    # endif debug

    if debug_f:
        print("ofs_mask=", "{0:016b}".format(ofs_mask))
    # endif debug

    # clear bits in register

    dst = dst & ofs_mask

    # place bits in register

    dst = dst | field

    return dst


# end modifyBM
#
# ----------------------------------
#
def BMofNumber(src, bidx, count):
    dst = 0
    debug_f = False

    ii = 0
    while ii < count:
        if debug_f:
            print("dst=", hex(dst))
        # endif

        test_bit = testBit(src, bidx + ii)

        if debug_f:
            print("test_bit=", test_bit)

        if test_bit > 0:
            dst = setBit(dst, ii)
            if debug_f:
                print("bit=", hex(test_bit))
            # endif debug

        # endif

        ii = ii + 1

    # end while

    if debug_f:
        print("dst=", hex(dst))
    # endif

    return dst


# end BMofNumber
#
# ----------------------------------
#
def parseBytes(theData, count):
    """Splits bytes into high and low bytes."""
    ret_bytes = bytearray()

    ii = 0
    while ii < count:
        elem = theData & 0xFF

        ret_bytes.append(elem)

        theData = theData >> 8

        ii = ii + 1

    return ret_bytes


# end parseBytes
#
# ------------------------------------------------------------------------------
#
# RegFREQ is the OSCin Freq
# RefDoubler: 0 = bypassed, 1 = enabled
# Ref Multipler: 1 = bypassed, 3,5,7 only allowed
# Ref Pre-Divider: 1 = bypass max = 128
# Ref Post-Divider: 1= bypass max = 255
class LMX2820:
    def __init__(self, RefFREQ, RefDoubler, RefMultipler, PreRDiv, PostRDiv):
        self.RefFREQ = RefFREQ
        self.RefDoubler = RefDoubler
        self.RefMultipler = RefMultipler
        self.PreRDiv = PreRDiv
        self.PostRDiv = PostRDiv
        self.reg = [0] * 123
        self.newreg = [0] * 123  # for programming hard-coded register maps
        self.DividerLUT = [2, 4, 8, 16, 32, 64, 128]
        self.reg[0] = (
            0b0100000001110000  # Reset register PFD = 100 MHz Setting Fcal HPFD and LPFD ADJ. F_Cal is 1
        )
        self.reg[2] = 0b1011001111101000  # Setting clock Div value
        self.reg[11] = 0b0000011000000010  # Ref doubler bypassed
        self.reg[12] = 0b0000010000001000  # Ref multiplier bypassed
        self.reg[13] = 0b0000000000111000  # Ref Post R Divider set to 1
        self.reg[14] = 0b0011000000000001  # Ref Pre R Divider set to 1
        if self.RefDoubler != 1 and self.RefDoubler != 0:  # CHekcing value of OSC_2x
            self.RefDoubler = 0  # Ensuring only states 0 and 1 are possible
        if (
            self.RefMultipler != 1
            and self.RefMultipler != 3
            and self.RefMultipler != 5
            and self.RefMultipler > 7
        ):  # Testing if requested multiplcation factor is valid
            print("Ref multiplier out of range (1,3,5,7 allowed). set to bypass, 1")
            self.RefMultipler = 1  # If value is out of range set to bypass
        if (
            self.PreRDiv < 1 or self.PreRDiv > 4095
        ):  # Testing if requested pre-divider value is valid
            print("Ref pre divider out  of range (1-128). Set to bypass, 1")
            self.PreRDiv = 1  # If out of range, set to bypass
        if (
            self.PostRDiv < 1 or self.PostRDiv > 255
        ):  # Checking if post divider is in valid range
            print("Ref pre divider out  of range (1-128). Set to bypass, 1")
            self.PostRDiv = 1  # If out of range, set to bypass
        Fpfd = self.RefFREQ * (
            ((1 + self.RefDoubler) * self.RefMultipler) / (self.PreRDiv * self.PostRDiv)
        )  # Calculating Phase Frequency detector frequency
        if (
            Fpfd > 225e6 or Fpfd < 5e6
        ):  # If Fpfd is out of range, set reference chain to bypass all
            print(
                "Phase frequency detector frequency too high. Must be <=225 MHz and  >=5 MHz. Setting multiplier and dividres to bypass."
            )
            self.RefDoubler == 0
            self.RefMultipler = 1
            self.PreRDiv = 1
            self.PostRDiv = 1
        else:  # If the Fpfd is value, set the registers for the the refernece chain
            if self.RefDoubler == 1:
                self.reg[11] = modifyBM(
                    self.reg[11], 1, 4, 1
                )  # Adjusting register 11 accordingly for the Ref multiply by 2
            self.reg[12] = modifyBM(
                self.reg[12], self.RefMultipler, 10, 3
            )  # Setting new Ref Multipleir value
            self.reg[14] = modifyBM(
                self.reg[14], self.PreRDiv, 0, 12
            )  # Setting new Ref pre divider value
            self.reg[13] = modifyBM(
                self.reg[13], self.PostRDiv, 5, 8
            )  # Setting new Ref post divider value
            if Fpfd <= 100e6:  # Setting the FCal_Hpfd_ADJ according to the Fpfd
                self.reg[0] = modifyBM(self.reg[0], 0, 9, 2)  #
            elif 100e6 < Fpfd and Fpfd <= 150e6:  #
                self.reg[0] = modifyBM(self.reg[0], 1, 9, 2)  #
            elif 150e6 < Fpfd and Fpfd <= 200e6:  #
                self.reg[0] = modifyBM(self.reg[0], 2, 9, 2)  #
            elif 200e6 < Fpfd:  #
                self.reg[0] = modifyBM(self.reg[0], 3, 9, 2)  #
            if Fpfd >= 10e6:  # Setting FCal_Lpfd_ADJ according to Fpfd
                self.reg[0] = modifyBM(self.reg[0], 0, 7, 2)  #
            elif 10e6 > Fpfd and Fpfd >= 5e6:  #
                self.reg[0] = modifyBM(self.reg[0], 1, 7, 2)  #
            elif 5e6 > Fpfd and Fpfd >= 2.5e6:  #
                self.reg[0] = modifyBM(self.reg[0], 2, 7, 2)  #
            elif 2.5e6 > Fpfd:  #
                self.reg[0] = modifyBM(self.reg[0], 3, 7, 2)  #
            if (
                self.RefFREQ <= 200e6
            ):  # Setting Cal_Clk_Divider based on input reference frequency
                self.reg[2] = modifyBM(self.reg[2], 0, 12, 3)  #
            elif 200e6 < self.RefFREQ and self.RefFREQ <= 400e6:  #
                self.reg[2] = modifyBM(self.reg[2], 1, 12, 3)  #
            elif 400e6 < self.RefFREQ and self.RefFREQ <= 800e6:  #
                self.reg[2] = modifyBM(self.reg[2], 2, 12, 3)  #
            elif 800e6 < self.RefFREQ:  #
                self.reg[2] = modifyBM(self.reg[2], 3, 12, 3)  #


# end __init__
#
# ----------------------------------
#
# Function to intilaize the register for the LMX and the CS pin for SPI
# Still needs to be transmitted to chip properly
# Commented registerd get defined in class defintion
def LMX2820InitRegs(LMX):
    LMX.reg[0] = (
        0b0100000001110000  # Reset register PFD = 100 MHz Setting Fcal HPFD and LPFD ADJ.
    )
    LMX.reg[1] = 0b0101011110100000  # Enable if doubler is engaged
    LMX.reg[2] = 0b1011001111101000  # Setting clock Div value
    LMX.reg[3] = 0x41  # Reserved
    LMX.reg[4] = 0x4204  # Reserved
    LMX.reg[5] = 0x32  # Reserved        Reset:0x3832
    LMX.reg[6] = 0xA43  # ACAL_CMP_DLY
    LMX.reg[7] = 0x00  # Reserved        Reset:0xC8
    LMX.reg[8] = 0xC802  # Reserved
    LMX.reg[9] = 0x05  # Reserved
    LMX.reg[10] = 0x00  # Manual PFD_DLY     Reset: 0b0000011000000011
    LMX.reg[11] = 0b0000011000000010  # Ref doubler bypassed
    LMX.reg[12] = 0b0000010000001000  # Ref multiplier bypassed
    LMX.reg[13] = 0b0000000000111000  # Ref Post R Divider set to 1
    LMX.reg[14] = 0b0011000000000001  # Ref Pre R Divider set to 1
    LMX.reg[15] = (
        0b0010000000000001  # Setting negative polairty for PFD charge pump to nomral operation
    )
    LMX.reg[16] = (
        0b0001011100011100  # Setting charge pump current   Reset: 0b0010011100011100
    )
    LMX.reg[17] = (
        0b0001010111000000  # Setting lock det type to continious  Reset: 1010001000000
    )
    LMX.reg[18] = 0x3E8  # Lock Detect assertion delay
    LMX.reg[19] = 0b0010000100100000  # Disabling temperature sensor
    LMX.reg[20] = 0b0010011100101100  # VCO_DACISET
    LMX.reg[21] = 0x1C64  # Reserved
    LMX.reg[22] = 0xE2BF  # Seting VCO core 7 as core to Cal
    LMX.reg[23] = 0b0001000100000010  # Disabling VCO_SEL_Force
    LMX.reg[24] = 0xE34  # Reserved
    LMX.reg[25] = 0x624  # Reserved
    LMX.reg[26] = 0xDB0  # Reserved
    LMX.reg[27] = 0x8001  # Reserved
    LMX.reg[28] = 0x639  # Reserved
    LMX.reg[29] = 0x318C  # Reserved
    LMX.reg[30] = 0xB18C  # Reserved
    LMX.reg[31] = 0x401  # Reserved
    LMX.reg[32] = (
        0b0001000000000001  # Setting Both Output vhannel dividers to 0 (Div by 2)
    )
    LMX.reg[33] = 0x00  # Reserved
    LMX.reg[34] = 0b0000000000010000  # Disabling External VCO and LoopBack
    LMX.reg[35] = 0b0011000100000000  # Enable Mash Reset and Set MASH order
    LMX.reg[36] = 60  # Setting INT value
    LMX.reg[37] = (
        0x500  # Setting PFD_DEL, only active whe PFD_DLY_MANUAL is 1, which it's NOT
    )
    LMX.reg[38] = 0x00  # MSBs PLL_DEN
    LMX.reg[39] = 0x3E8  # LSBs of PLL_DEN
    LMX.reg[40] = 0x00  # MSBs MASH seed
    LMX.reg[41] = 0x00  # LSBs MASH seed
    LMX.reg[42] = 0x00  # MSBs PLL_NUM
    LMX.reg[43] = 0x00  # LSBs PLL_NUM
    LMX.reg[44] = 0x00  # MSBs INSTCAL_PLL  2^32*(PLL_NUM/PLL_DEN)
    LMX.reg[45] = 0x00  # LSBs INSTCAL_PLL
    LMX.reg[46] = 0x300  # Reserved
    LMX.reg[47] = 0x300  # Reserved
    LMX.reg[48] = 0x4180  # Reserved
    LMX.reg[49] = 0x00  # Reserved
    LMX.reg[50] = 0x50  # Reserved
    LMX.reg[51] = 0x203F  # Reserved
    LMX.reg[52] = 0x00  # Reserved
    LMX.reg[53] = 0x00  # Reserved
    LMX.reg[54] = 0x00  # Reserved
    LMX.reg[55] = 0x02  # Reserved
    LMX.reg[56] = 0x01  # Reserved
    LMX.reg[57] = 0x01  # Disabling PFDIN input
    LMX.reg[58] = 0x00  # Reserved
    LMX.reg[59] = 0x1388  # Reserved
    LMX.reg[60] = 0x1F4  # Reserved
    LMX.reg[61] = 0x3E8  # Reserved
    LMX.reg[62] = 0x00  # MSBs MASH_RST_COUNT
    LMX.reg[63] = 0xC350  # LSBs MASH_RST_COUNT
    LMX.reg[64] = 0x4080  # Disabling System REF
    LMX.reg[65] = 0x01  # Setting System Ref divider
    LMX.reg[66] = (
        0x3F  # Setting 2 of 4 JESD registers, sum of 2 registers must be equal or greater than 63
    )
    LMX.reg[67] = 0x00  # Setting last 2 JESD regiseres as  zero
    LMX.reg[68] = 0x00  # Disabling Phase Sync
    LMX.reg[69] = 0x11  # Power Down System Refoutput buffer
    LMX.reg[70] = (
        0x1E  # Setting Registers as double buffered, Changes to regs will only be enabled after a write to R0
    )
    LMX.reg[71] = 0x00  # Reserved
    LMX.reg[72] = 0x00  # Reserved
    LMX.reg[73] = 0x00  # Reserved
    LMX.reg[74] = 0x00  # Reserved
    LMX.reg[75] = 0x00  # Reserved
    LMX.reg[76] = 0x00  # Reserved
    LMX.reg[77] = 0b0000011000001000  # Mute Pin Polarity
    LMX.reg[78] = 0x01  # Output A enabled and OutA Mux set to VCO
    LMX.reg[79] = 0x11E  # Power Down B, OutB Mux set to VCO, Max power outputA
    LMX.reg[80] = 0x1C0  # Output B max power
    LMX.reg[81] = 0x00  # Reserved
    LMX.reg[82] = 0x00  # Reserved
    LMX.reg[83] = 0xF00  # Reserved
    LMX.reg[84] = 0x40  # Reserved
    LMX.reg[85] = 0x00  # Reserved
    LMX.reg[86] = 0x40  # Reserved
    LMX.reg[87] = 0xFF00  # Reserved
    LMX.reg[88] = 0x3FF  # Reserved
    LMX.reg[89] = 0x00  # Reserved
    LMX.reg[90] = 0x00  # Reserved
    LMX.reg[91] = 0x00  # Reserved
    LMX.reg[92] = 0x00  # Reserved
    LMX.reg[93] = 0x1000  # Reserved
    LMX.reg[94] = 0x00  # Reserved
    LMX.reg[95] = 0x00  # Reserved
    LMX.reg[96] = 0x17F8  # Reserved
    LMX.reg[97] = 0x00  # Reserved
    LMX.reg[98] = 0x1C80  # Reserved
    LMX.reg[99] = 0x19B9  # Reserved
    LMX.reg[100] = 0x533  # Reserved
    LMX.reg[101] = 0x3E8  # Reserved
    LMX.reg[102] = 0x28  # Reserved
    LMX.reg[103] = 0x14  # Reserved
    LMX.reg[104] = 0x14  # Reserved
    LMX.reg[105] = 0x0A  # Reserved
    LMX.reg[106] = 0x00  # Reserved
    LMX.reg[107] = 0x00  # Reserved
    LMX.reg[108] = 0x00  # Reserved
    LMX.reg[109] = 0x00  # Reserved
    LMX.reg[110] = 0x1F  # Reserved
    LMX.reg[111] = 0x00  # Reserved
    LMX.reg[112] = 0xFFFF  # Reserved
    LMX.reg[113] = 0x00  # Reserved
    LMX.reg[114] = 0x00  # Reserved
    LMX.reg[115] = 0x00  # Reserved
    LMX.reg[116] = 0x00  # Reserved
    LMX.reg[117] = 0x00  # Reserved
    LMX.reg[118] = 0x00  # Reserved
    LMX.reg[119] = 0x00  # Reserved
    LMX.reg[120] = 0x00  # Reserved
    LMX.reg[121] = 0x00  # Reserved
    LMX.reg[122] = 0x00  # Reserved

    # Use this register map (newreg) to tune to 6.1266 GHz

    # !!! Reg 0 is 0x6470 for Default, 0x6070 for 10MHz !!!
    LMX.newreg[0] = (
        0x6070  # 0b0100000001110000     #Reset register PFD = 100 MHz Setting Fcal HPFD and LPFD ADJ.
    )
    # !!!

    LMX.newreg[1] = 0x57A0  # 0b0101011110100000     #Enable if doubler is engaged

    # !!! Reg 2 is 0x81F4 for Default, 0x8032 for 10MHz !!!
    LMX.newreg[2] = 0x8032  # 0b1011001111101000     #Setting clock Div value
    # !!!

    LMX.newreg[3] = 0x0041  # Reserved
    LMX.newreg[4] = 0x4204  # Reserved
    LMX.newreg[5] = 0x0032  # Reserved        Reset:0x3832
    LMX.newreg[6] = 0x0A43  # ACAL_CMP_DLY
    LMX.newreg[7] = 0x0000  # Reserved        Reset:0xC8
    LMX.newreg[8] = 0xC802  # Reserved
    LMX.newreg[9] = 0x0005  # Reserved
    LMX.newreg[10] = 0x0000  # Manual PFD_DLY     Reset: 0b0000011000000011
    LMX.newreg[11] = 0x0612  # 0b0000011000000010    #Ref doubler bypassed
    LMX.newreg[12] = 0x0408  # 0b0000010000001000    #Ref multiplier bypassed
    LMX.newreg[13] = 0x0038  # 0b0000000000111000    #Ref Post R Divider set to 1
    LMX.newreg[14] = 0x3001  # 0b0011000000000001    #Ref Pre R Divider set to 1
    LMX.newreg[15] = (
        0x2001  # 0b0010000000000001    #Setting negative polairty for PFD charge pump to nomral operation
    )
    LMX.newreg[16] = (
        0x171C  # 0b0001011100011100    #Setting charge pump current   Reset: 0b0010011100011100
    )
    LMX.newreg[17] = (
        0x15C0  # 0b0001010111000000    #Setting lock det type to continious  Reset: 1010001000000
    )
    LMX.newreg[18] = 0x0000  # Lock Detect assertion delay
    LMX.newreg[19] = 0x2120  # 0b0010000100100000    #Disabling temperature sensor
    LMX.newreg[20] = 0x272C  # 0b0010011100101100    #VCO_DACISET
    LMX.newreg[21] = 0x1C64  # Reserved
    LMX.newreg[22] = 0xE2BF  # Seting VCO core 7 as core to Cal
    LMX.newreg[23] = 0x1102  # 0b0001000100000010    #Disabling VCO_SEL_Force
    LMX.newreg[24] = 0x0E34  # Reserved
    LMX.newreg[25] = 0x0624  # Reserved
    LMX.newreg[26] = 0x0DB0  # Reserved
    LMX.newreg[27] = 0x8001  # Reserved
    LMX.newreg[28] = 0x0639  # Reserved
    LMX.newreg[29] = 0x318C  # Reserved
    LMX.newreg[30] = 0xB18C  # Reserved
    LMX.newreg[31] = 0x0401  # Reserved
    LMX.newreg[32] = (
        0x1001  # 0b0001000000000001    #Setting Both Output vhannel dividers to 0 (Div by 2)
    )
    LMX.newreg[33] = 0x0000  # Reserved
    LMX.newreg[34] = (
        0x0010  # 0b0000000000010000    #Disabling External VCO and LoopBack
    )
    LMX.newreg[35] = (
        0x3100  # 0b0011000100000000    #Enable Mash Reset and Set MASH order
    )

    # !!! Reg 36 is 0x001E for Default, 0x012C for 10MHz !!!
    LMX.newreg[36] = 0x012C  # Setting INT value
    # !!!

    LMX.newreg[37] = (
        0x0500  # Setting PFD_DEL, only active whe PFD_DLY_MANUAL is 1, which it's NOT
    )
    LMX.newreg[38] = 0x0000  # MSBs PLL_DEN
    LMX.newreg[39] = 0x03E8  # LSBs of PLL_DEN
    LMX.newreg[40] = 0x0000  # MSBs MASH seed
    LMX.newreg[41] = 0x0000  # LSBs MASH seed
    LMX.newreg[42] = 0x0000  # MSBs PLL_NUM
    LMX.newreg[43] = 0x0279  # LSBs PLL_NUM
    LMX.newreg[44] = 0xA20C  # MSBs INSTCAL_PLL  2^32*(PLL_NUM/PLL_DEN)
    LMX.newreg[45] = 0x45BA  # LSBs INSTCAL_PLL
    LMX.newreg[46] = 0x0300  # Reserved
    LMX.newreg[47] = 0x0300  # Reserved
    LMX.newreg[48] = 0x4180  # Reserved
    LMX.newreg[49] = 0x0000  # Reserved
    LMX.newreg[50] = 0x0080  # Reserved
    LMX.newreg[51] = 0x203F  # Reserved
    LMX.newreg[52] = 0x0000  # Reserved
    LMX.newreg[53] = 0x0000  # Reserved
    LMX.newreg[54] = 0x0000  # Reserved
    LMX.newreg[55] = 0x0002  # Reserved
    LMX.newreg[56] = 0x0001  # Reserved
    LMX.newreg[57] = 0x0001  # Disabling PFDIN input
    LMX.newreg[58] = 0x0000  # Reserved
    LMX.newreg[59] = 0x1388  # Reserved
    LMX.newreg[60] = 0x01F4  # Reserved
    LMX.newreg[61] = 0x03E8  # Reserved
    LMX.newreg[62] = 0x0000  # MSBs MASH_RST_COUNT
    LMX.newreg[63] = 0xC350  # LSBs MASH_RST_COUNT
    LMX.newreg[64] = 0x0080  # Disabling System REF
    LMX.newreg[65] = 0x0000  # Setting System Ref divider
    LMX.newreg[66] = (
        0x003F  # Setting 2 of 4 JESD registers, sum of 2 registers must be equal or greater than 63
    )
    LMX.newreg[67] = 0x1000  # Setting last 2 JESD regiseres as  zero
    LMX.newreg[68] = 0x0020  # Disabling Phase Sync
    LMX.newreg[69] = 0x0011  # Power Down System Refoutput buffer
    LMX.newreg[70] = (
        0x000E  # Setting Registers as double buffered, Changes to regs will only be enabled after a write to R0
    )
    LMX.newreg[71] = 0x0000  # Reserved
    LMX.newreg[72] = 0x0000  # Reserved
    LMX.newreg[73] = 0x0000  # Reserved
    LMX.newreg[74] = 0x0000  # Reserved
    LMX.newreg[75] = 0x0000  # Reserved
    LMX.newreg[76] = 0x0000  # Reserved
    LMX.newreg[77] = 0x0608  # 0b0000011000001000    #Mute Pin Polarity
    LMX.newreg[78] = 0x0001  # Output A enabled and OutA Mux set to VCO
    LMX.newreg[79] = 0x011E  # Power Down B, OutB Mux set to VCO, Max power outputA
    LMX.newreg[80] = 0x01C0  # Output B max power
    LMX.newreg[81] = 0x0000  # Reserved
    LMX.newreg[82] = 0x0000  # Reserved
    LMX.newreg[83] = 0x0F00  # Reserved
    LMX.newreg[84] = 0x0040  # Reserved
    LMX.newreg[85] = 0x0000  # Reserved
    LMX.newreg[86] = 0x0040  # Reserved
    LMX.newreg[87] = 0xFF00  # Reserved
    LMX.newreg[88] = 0x03FF  # Reserved
    LMX.newreg[89] = 0x0000  # Reserved
    LMX.newreg[90] = 0x0000  # Reserved
    LMX.newreg[91] = 0x0000  # Reserved
    LMX.newreg[92] = 0x0000  # Reserved
    LMX.newreg[93] = 0x1000  # Reserved
    LMX.newreg[94] = 0x0000  # Reserved
    LMX.newreg[95] = 0x0000  # Reserved
    LMX.newreg[96] = 0x17F8  # Reserved
    LMX.newreg[97] = 0x0000  # Reserved
    LMX.newreg[98] = 0x1C80  # Reserved
    LMX.newreg[99] = 0x19B9  # Reserved
    LMX.newreg[100] = 0x0533  # Reserved
    LMX.newreg[101] = 0x03E8  # Reserved
    LMX.newreg[102] = 0x0028  # Reserved
    LMX.newreg[103] = 0x0014  # Reserved
    LMX.newreg[104] = 0x0014  # Reserved
    LMX.newreg[105] = 0x000A  # Reserved
    LMX.newreg[106] = 0x0000  # Reserved
    LMX.newreg[107] = 0x0000  # Reserved
    LMX.newreg[108] = 0x0000  # Reserved
    LMX.newreg[109] = 0x0000  # Reserved
    LMX.newreg[110] = 0x001F  # Reserved
    LMX.newreg[111] = 0x0000  # Reserved
    LMX.newreg[112] = 0xFFFF  # Reserved
    LMX.newreg[113] = 0x0000  # Reserved
    LMX.newreg[114] = 0x0000  # Reserved
    LMX.newreg[115] = 0x0000  # Reserved
    LMX.newreg[116] = 0x0000  # Reserved
    LMX.newreg[117] = 0x0000  # Reserved
    LMX.newreg[118] = 0x0000  # Reserved
    LMX.newreg[119] = 0x0000  # Reserved
    LMX.newreg[120] = 0x0000  # Reserved
    LMX.newreg[121] = 0x0000  # Reserved
    LMX.newreg[122] = 0x0000  # Reserved


# end LMX2820InitRegs
#
# ----------------------------------
#
# Calculating new INT NUM and DEN values for a new frequency request within VCO range
# VCO range limits 5.65 - 11.3 GHz
def LMX2820calcIntNumDen(LMX, newFreq):
    # newParams = fractions()
    Fpfd = int(
        LMX.RefFREQ
        * ((1 + LMX.RefDoubler) * LMX.RefMultipler)
        // (LMX.PreRDiv * LMX.PostRDiv)
    )  # Calculating Phase Frequency detector frequency
    # print("PFD frequency: ",Fpfd)
    if (
        newFreq < 5.56e9 or newFreq > 11.3e9
    ):  # Testing if requested frequency falls within VCO range
        INT = 60  # If not, set the output values for default frequency of 6 GHz
        NUM = 0
        DEM = 1
        print("Frequency outside VCO range")
    else:
        """
        createdReducedFraction(newParams,newFreq/Fpfd)
        INT = newParams.WHOLE
        NUM = newParams.NUM
        DEM = newParams.DEM
        """
        # print("newFreq,Fpfd=",newFreq,Fpfd)                                      # diagnostic, params into next fcn must be int()
        INT = newFreq // Fpfd
        remainder = newFreq - INT * Fpfd

        if remainder == 0:
            NUM = 0
            DEN = 1
        else:
            # Fractional numerator = (newFreq − INT*Fpfd), denominator = Fpfd
            NUM = remainder
            DEN = Fpfd
    RFout = Fpfd * (INT + NUM / DEN)
    return [INT, NUM, DEN, RFout]


# end LMX2820calcIntNumDen
#
# ----------------------------------
#
# Setting the registers for a new frequency request
# Sets dividers and mux for frequency requests down to 44.140265 MHz
# Max frequency 22.6 GHz.
def LMX2820setREGSforNewFreq(LMX, newFreq):
    global SIMU

    outputDividerIndex = 0  # Defining an index to use for output divider
    if (
        newFreq < 44.140265e6 or newFreq > 22.6e9
    ):  # Making sure the requested freuqency falls withing accetable range
        realFreq = 6e9  # If not, defining the real freuency as 6 GHz
        LMX.reg[78] = modifyBM(LMX.reg[78], 1, 0, 2)  # Setting output_A mux to VCO
        LMX.reg[1] = modifyBM(
            LMX.reg[1], 0, 1, 1
        )  # Setting InstaCal_2x to normal operation
    elif (
        newFreq >= 5.56e9 and newFreq <= 11.3e9
    ):  # If the requested freuqnecy falls withing the VCO range,
        realFreq = int(newFreq)  # define the real ferquency as the reqeusted frequency
        LMX.reg[78] = modifyBM(LMX.reg[78], 1, 0, 2)  # Setting output_A mux to VCO
        LMX.reg[1] = modifyBM(
            LMX.reg[1], 0, 1, 1
        )  # Setting InstaCal_2x to normal operation
    elif (
        newFreq > 11.3e9 and newFreq <= 22.6e9
    ):  # If the requested frequency falls within 2x VCO range,
        realFreq = int(newFreq / 2)  # Set realFreq as half desired frequency
        LMX.reg[78] = modifyBM(LMX.reg[78], 2, 0, 2)  # Setting output_A mux to doubler
        LMX.reg[1] = modifyBM(
            LMX.reg[1], 1, 1, 1
        )  # Setting InstaCal_2x to doubler engaged operation
    else:  # If the requested freuqnecy needs the divider outputs
        LMX.reg[78] = modifyBM(
            LMX.reg[78], 0, 0, 2
        )  # Set output_A mux to Channel Divider
        while (
            newFreq * LMX.DividerLUT[outputDividerIndex] < 5.56e9
        ):  # Loop through the divider lookup table to find the necessary divider to reach the VCO range
            outputDividerIndex = outputDividerIndex + 1
        realFreq = int(
            newFreq * LMX.DividerLUT[outputDividerIndex]
        )  # Define the real frequency as the requested frequency * the necessary divider
        LMX.reg[32] = modifyBM(
            LMX.reg[32], outputDividerIndex, 6, 3
        )  # Setting VCO divider value
        LMX.reg[1] = modifyBM(
            LMX.reg[1], 0, 1, 1
        )  # Setting InstaCal_2x to normal operation
    #    print(newFreq,realFreq, outputDividerIndex,BMofNumber(LMX.reg[78],0,2))    #Debug
    newParams = LMX2820calcIntNumDen(
        LMX, realFreq
    )  # Calculating new INT, NUM, DEN for the real frequency
    #    print("INT, NUM, DEM, RealFreq")
    #    print(newParams)                                                       #Debug
    if newParams[1] == 0:
        LMX.reg[35] = modifyBM(
            LMX.reg[35], 0, 7, 2
        )  # Seting Mash_Order to zero if system in is Interger mode (NUM = 0)
        LMX.reg[35] = modifyBM(
            LMX.reg[35], 0, 12, 1
        )  # Seting Mash_Reset to zero if system in is Interger mode (NUM = 0)
    elif newParams[2] <= 7 and newParams[1] != 0:
        LMX.reg[35] = modifyBM(
            LMX.reg[35], 1, 7, 2
        )  # Seting MASH modulator to first order if DEN < 7
        LMX.reg[35] = modifyBM(
            LMX.reg[35], 1, 12, 1
        )  # Seting Mash_Reset to zero if system in is fractional mode
    elif newParams[2] > 7 and newParams[1] != 0:
        LMX.reg[35] = modifyBM(
            LMX.reg[35], 2, 7, 2
        )  # Seting MASH modulator to second order if DEN > 7
        LMX.reg[35] = modifyBM(
            LMX.reg[35], 1, 12, 1
        )  # Seting Mash_Reset to zero if system in is fractional mode
    LMX.reg[42] = BMofNumber(newParams[1], 16, 16)  # Setting MSBs of NUM
    LMX.reg[43] = BMofNumber(newParams[1], 0, 16)  # Setting LSBs of NUM
    LMX.reg[38] = BMofNumber(newParams[2], 16, 16)  # Setting MSBs of DEN
    LMX.reg[39] = BMofNumber(newParams[2], 0, 16)  # Setting LSBs of DEN
    LMX.reg[36] = BMofNumber(newParams[0], 0, 15)  # Setting INT
    INSTCAL_PLL = int(
        pow(2, 32) * (newParams[1] / newParams[2])
    )  # calculating new INSCAL_PLL number
    LMX.reg[44] = BMofNumber(INSTCAL_PLL, 16, 16)  # Setting INSTCAL MSB
    LMX.reg[45] = BMofNumber(INSTCAL_PLL, 0, 16)  # Setting INSTCAL LSB


# end LMX2820setREGSforNewFreq
#
# ----------------------------------
#
# Write a single register to the LMX
def LMX2820WriteSingleRegister(spi, CS, LMX, address, NewValue):
    debug_f = False

    b2 = address & 0xFF  # 1 byte address
    b1 = (NewValue & 0xFF00) >> 8  # MSB
    b0 = NewValue & 0xFF  # LSB

    icmd = [b2, b1, b0]  # array of type int
    bcmd = bytes(icmd)  # array of tkype bytes

    try:
        CS.switch_to_output(value=False)
    except Exception:
        if SIMU:
            pass
        else:
            raise

    # time.sleep(0.1)

    if debug_f:
        print("Register=", address, "byts2write=", [hex(x) for x in bcmd])
    # endif

    try:
        spi.write(bcmd)
    except Exception:
        if debug_f:
            print("failed to write reg: ", address)
        if SIMU:
            pass
        else:
            raise
    try:
        CS.switch_to_output(value=True)
    except Exception:
        if SIMU:
            pass
        else:
            raise


# end LMX2820WriteSingleRegister
#
# ----------------------------------
#
# Function to start up the LMX
# Initializes registers to 6 GHz and writes regs in the order described in the datasheet
def LMX2820StartUp(LMX, spi, cs):
    LMX2820InitRegs(LMX)  # Writting inital register values
    LMX2820setREGSforNewFreq(LMX, 6e9)  # Setting registers to 6 GHz
    LMX.reg[0] = modifyBM(LMX.reg[0], 1, 1, 1)  # Writting 1 to reset register
    LMX2820WriteSingleRegister(spi, cs, LMX, 0, LMX.reg[0])  # Tramsmitting reg 0
    LMX.reg[0] = modifyBM(LMX.reg[0], 0, 1, 1)  # Writting 0 to reset register
    LMX2820WriteSingleRegister(spi, cs, LMX, 0, LMX.reg[0])  # Tramsmitting reg 0
    for x in range(
        0, 117
    ):  # Transmitting initizlied registers from top (112) to bottom (0)
        LMX2820WriteSingleRegister(spi, cs, LMX, 116 - x, LMX.reg[116 - x])
    time.sleep(10e-3)  # Waiting 10 ms per datasheet recommendation page 40
    LMX.reg[0] = modifyBM(LMX.reg[0], 1, 4, 1)  # Writting a 1 in FCal_En
    LMX2820WriteSingleRegister(spi, cs, LMX, 0, LMX.reg[0])  # Tramsmitting reg 0


# end LMX2820StartUp
#
# ----------------------------------


def LMX2820WriteRegisterMap(LMX, spi, cs):
    for x in range(0, 117):
        LMX2820WriteSingleRegister(spi, cs, LMX, 116 - x, LMX.newreg[116 - x])
    time.sleep(10e-3)


# end LMX2820WriteRegisterMap
# ----------------------------------


#
# Function to change the frequency fo the LMX
# Full frequency range allowed, 44.2 MHz to 22.6GHz
# Function writes necessary registers and sends new values to the LMX
# Function ends by writting a 1 to the FCAl to lock new frequency value
#
def LMX2820ChangeFreq(spi, cs, LMX, newFreq):
    LMX2820setREGSforNewFreq(LMX, newFreq)
    for x in range(0, 80):
        LMX2820WriteSingleRegister(spi, cs, LMX, 79 - x, LMX.reg[79 - x])
    time.sleep(10e-3)
    LMX.reg[0] = modifyBM(LMX.reg[0], 1, 4, 1)  # Writting a 1 in FCal_En
    LMX2820WriteSingleRegister(spi, cs, LMX, 0, LMX.reg[0])


# end LMX2820ChangeFreq


#
# Function to call LMX2820ChangeFreq based on command line input
# Currently only operates between 1 and 22.6 GHz
#
def tune():
    raw = input("Enter Frequency in GHz (1 to 22.6 GHz): ")
    try:
        newFreq = float(raw) * 1e9
    except (ValueError, TypeError):
        print(f"'{raw}' is not a valid frequency")
        return tune()

    if int(round(float(raw) * 1e9)) % 1000 != 0:
        print("Can not tune to a precision of less than 1 kHz")
        return tune()

    if not (1e9 <= newFreq <= 22.6e9):
        print("Frequency must be in range")
        return tune()

    ghz = newFreq / 1e9
    print(f"Tuning to {ghz} GHz")
    LMX2820ChangeFreq(spi, CSpin, LMX, int(newFreq))
    print(f"----------Tuned to {ghz} GHz----------\n")

    return tune()


#
#
# ----------------------------------
#     main
# ----------------------------------
#
if __name__ == "__main__":
    if SIMU:
        print("\n*** FTDI SIMULATION !!! ***\n")

    # ctrl = SpiController(5)

    try:
        spi = busio.SPI(
            board.SCK,  # clock
            board.MOSI,  # mosi
            board.MISO,
        )  # miso
    except Exception as eobj:
        if SIMU:
            print("busio.SPI() Exception:", eobj)
        else:
            raise

    try:
        CSpin = DigitalInOut(board.D4)
        CSpin.switch_to_output(value=True)
    except Exception as eobj:
        if SIMU:
            print("busio.SPI() Exception:", eobj)
        else:
            raise

    print("Initializing register map")
    LMX = LMX2820(
        10e6,  # RefFREQ                  # initiation of class __init__
        0,  # RefDoubler 0 -> bypasses
        1,  # RefMultipler
        1,  # PreRDiv
        1,
    )  # PostRDiv

    print("Writing register map to LMX2820")
    LMX2820StartUp(LMX, spi, CSpin)
    print("-----Tuned to 6.0 GHz-----")

    tune()

#
# ----------------------------------
#     END OF FILE
# ----------------------------------
#
