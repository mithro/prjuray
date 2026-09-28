# Project U-Ray

Project U-Ray is an attempt at documenting the bitstream format for the
[Xilinx Ultrascale and Ultrascale+ parts](https://www.xilinx.com/products/technology/ultrascale.html)
including all parts from the following lines;
 * Kintex Ultrascale
 * Virtex Ultrascale
 * Zynq UltraScale MPSoC
 * Kintex UltraScale+
 * Virtex UltraScale+
 * Zynq UltraScale+ MPSoC

It takes a lot of the learning from
[Project X-Ray](https://github.com/SymbiFlow/prjxray) and
[Project Trellis](https://github.com/SymbiFlow/prjtrellis).

# Current status (2026-09-28)

The [`generic/`](generic/README.md) flow documents the bitstream of every
7-series, UltraScale and UltraScale+ die available in the **Vivado 2025.2
WebPACK** edition, using one architecture independent pipeline. No hand
written, per tile type fuzzers are involved. The steps are:

1. Generate random designs for each die.
2. Have Vivado report the *features* (PIPs, site and cell settings) each
   design uses.
3. Learn the tile grid (which frames and words belong to each tile) from
   the designs.
4. Correlate the features with the bits of the bitstreams (`mkdb.py`).

The resulting bit databases are checked against held-out designs that were
not used to build them (`check.py`).

Coverage: all 29 distinct dies (102 WebPACK devices; devices that share a
die share its tile grid and database), grouped into three bit databases:

| Database | Dies |
| -------- | ---- |
| Series7 (14) | xa7s15, xa7a12t, xa7s25, xa7a15t, xa7z010, xc7z012s, xa7s50, xa7z020, xa7a100t, xa7s100, xa7z030, xc7k70t, xc7k160t, xc7a200t |
| UltraScale (2) | xcku025, xcku035 |
| UltraScale+ (13) | xaau7p, xazu1eg, xaau10p, xazu2eg, xazu3teg, xcku3p, xczu4cg, xazu4ev, xcau20p, xck26, xazu7ev, xczu7cg, xcu25 |

`build/meta/die_groups.json` lists which device uses which die.

## How complete the databases are

Each database is checked against held-out designs. A set bit is
*undocumented* when no feature of that design explains it. A bit is
*unowned* when it isn't covered by any tile in the tile grid. The table
shows the median number of distinct undocumented bits per held-out design
(8-20 designs per die, each with 0.2-4 million set bits):

| Database | Median distinct undocumented bits per design | Unowned bits |
| -------- | ---------------------------------------------- | ------------ |
| Series7 | 2 (xa7s15) to 45 (xc7k160t); most dies 6-36 | 0, except xc7a200t (210 in 3 of 20 designs, GTP column placement, being fixed) |
| UltraScale+, large dies | 44 (xazu3teg) to 110 (xazu7ev) | 0, except xazu3teg (7) |
| UltraScale+, small dies and xcu25 | 260 (xazu1eg) to 511 (xazu2eg); xcu25 307 | 0 |
| UltraScale | xcku035 1050, xcku025 2109 | 462 / 868 |

Series7 is close to complete. The remaining bits are mostly rare routing
bits in INT/CLB tiles, plus the CFG_CENTER bits that depend on whether the
STARTUPE2 GTS pin is driven or tied. The new hard block pin driver features
(`<site>.PIN.<pin>=SIGNAL|GND|VCC`) are expected to explain the CFG_CENTER
bits once new design rounds are dumped with them.

UltraScale is the least complete. Recent fixes to the model of parked
(undriven) interconnect muxes cut xcku025's distinct undocumented bits in
the CI regression run from 78,097 to 49,446. The open classes are:

* INT and RCLK_INT_L leaf clock bits.
* XIPHY/HPIO unused pin programming.
* Designs that fail to place or route on xcku025.

The last full production check on xcku025 covered only 9 designs.

## Not done yet

* No independent review of the databases has been done yet. That review
  is the final acceptance step.
* The remaining undocumented bit classes listed above.
* The databases are built into `build/db/<arch>/` and are not committed to
  this repository.

See [`generic/README.md`](generic/README.md) for how to run the flow. It
covers the steps and tools, memory limits (`generic/vrun.sh`), the CI
regression (`generic/ci.sh`) and the rebuild scripts in `generic/runs/`.

# Target Parts

## Ultrascale

### Kintex UltraScale

#### Parts

 * TBD

#### Boards

 * TBD

### Virtex UltraScale

#### Parts

 * TBD

#### Boards

 * TBD

## Ultrascale+

### Kintex UltraScale+

#### Parts

 * TBD - Targetting XCKU11P?

#### Boards

 * TBD

### Virtex UltraScale+

#### Parts

 * TBD - Targetting XCVU13P?

#### Boards

 * TBD

### Zynq UltraScale+

#### Parts

 * Zynq Ultrascale+ MPSoC - **ZU3EG**

#### Boards

| Board | Maker | Price | Part |
| ----- | ----- | ----- | ---- |
| [Ultra96-V2 Zynq UltraScale+ ZU3EG Development Board (ULTRA96-V2-G)](https://www.avnet.com/shop/us/products/avnet-engineering-services/aes-ultra96-v2-g-3074457345638646173/) | ??? | Xilinx Zynq UltraScale+ MPSoC ZU3EG | $USD249 |
| [Genesys ZU: Zynq Ultrascale+ MPSoC Development Board](https://store.digilentinc.com/genesys-zu-zynq-ultrascale-mpsoc-development-board/) | Digilent | Xilinx Zynq UltraScale+ MPSoC ZU3EG | $USD1,149 |


## WebPack Parts

We have a goal of initially targeting parts supported by WebPack so that anyone
can contribute.

Vivado 2025.2 WebPACK supports the following UltraScale and UltraScale+
devices. Devices on the same line share a die, and all of them are covered
by the `generic/` flow:
 * Kintex UltraScale: XCKU025; XCKU035
 * Kintex UltraScale+: XCKU3P, XCKU5P
 * Artix UltraScale+: XCAU7P, XAAU7P; XCAU10P, XCAU15P, XAAU10P, XAAU15P;
   XCAU20P, XCAU25P
 * Zynq UltraScale+ MPSoC: XCZU1CG, XCZU1EG, XAZU1EG; XCZU2CG, XCZU2EG,
   XCZU3CG, XCZU3EG, XAZU2EG, XAZU3EG, XCK24; XCZU3TCG, XCZU3TEG, XAZU3TEG;
   XCZU4CG, XCZU4EG, XCZU5CG, XCZU5EG; XCZU4EV, XCZU5EV, XAZU4EV, XAZU5EV;
   XCZU7CG, XCZU7EG; XCZU7EV, XAZU7EV, XCU30; XCK26; XCU25

It also includes all Spartan-7, Artix-7 (up to XC7A200T), Kintex-7 (XC7K70T,
XC7K160T) and Zynq-7000 (up to XC7Z030) devices. See
`build/meta/die_groups.json` for the full list of 102 devices.

# Contributing

There are a couple of guidelines when contributing to Project U-Ray which are
listed here.

### Sending

All contributions should be sent as
[GitHub Pull requests](https://help.github.com/articles/creating-a-pull-request-from-a-fork/).

### License

All software (code, associated documentation, support files, etc) in the
Project U-Ray repository are licensed under the very permissive
[Apache-2.0 License](LICENSE). A copy can be found in the [`LICENSE`](LICENSE) file.

All new contributions must also be released under this license.

### Code of Conduct

By contributing you agree to the [code of conduct](CODE_OF_CONDUCT.md). We
follow the open source best practice of using the [Contributor
Covenant](https://www.contributor-covenant.org/) for our Code of Conduct.

### Sign your work

To improve tracking of who did what, we follow the Linux Kernel's
["sign your work" system](https://github.com/wking/signed-off-by).
This is also called a
["DCO" or "Developer's Certificate of Origin"](https://developercertificate.org/).

**All** commits are required to include this sign off and we use the
[Probot DCO App](https://github.com/probot/dco) to check pull requests for
this.

The sign-off is a simple line at the end of the explanation for the
patch, which certifies that you wrote it or otherwise have the right to
pass it on as a open-source patch.  The rules are pretty simple: if you
can certify the below:

        Developer's Certificate of Origin 1.1

        By making a contribution to this project, I certify that:

        (a) The contribution was created in whole or in part by me and I
            have the right to submit it under the open source license
            indicated in the file; or

        (b) The contribution is based upon previous work that, to the best
            of my knowledge, is covered under an appropriate open source
            license and I have the right under that license to submit that
            work with modifications, whether created in whole or in part
            by me, under the same open source license (unless I am
            permitted to submit under a different license), as indicated
            in the file; or

        (c) The contribution was provided directly to me by some other
            person who certified (a), (b) or (c) and I have not modified
            it.

	(d) I understand and agree that this project and the contribution
	    are public and that a record of the contribution (including all
	    personal information I submit with it, including my sign-off) is
	    maintained indefinitely and may be redistributed consistent with
	    this project or the open source license(s) involved.

then you just add a line saying

	Signed-off-by: Random J Developer <random@developer.example.org>

using your real name (sorry, no pseudonyms or anonymous contributions.)

You can add the signoff as part of your commit statement. For example:

    git commit --signoff -a -m "Fixed some errors."

*Hint:* If you've forgotten to add a signoff to one or more commits, you can use the
following command to add signoffs to all commits between you and the upstream
master:

    git rebase --signoff upstream/master

### Contributing to the docs

In addition to the above contribution guidelines, see the guide to
[updating the Project U-Ray docs](UPDATING-THE-DOCS.md).
