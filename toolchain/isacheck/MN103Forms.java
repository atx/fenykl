// Histogram of MN103 instruction forms in the current program, decoded by the ghidra-mn103 module.
// A form is the mnemonic plus operands with registers reduced to their class (Dn/An) and numbers to
// '#', plus the encoded length. With an offsets file (one decimal offset from the image base per
// line), those offsets are disassembled first without flow and then histogrammed; undecodable
// offsets are written as form "UNDECODABLE", and every decoded offset is listed in <insns.tsv>.
// Usage (headless): -postScript MN103Forms.java <out.tsv> [<offsets-file> <insns.tsv>]
// Output: form<TAB>length<TAB>count
// insns.tsv: offset<TAB>length<TAB>text<TAB>form
//@category MN103

import java.io.PrintWriter;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashMap;
import java.util.Map;
import java.util.TreeMap;
import java.util.regex.Pattern;

import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSet;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;
import ghidra.program.model.mem.MemoryBlock;

public class MN103Forms extends GhidraScript {
	private static final Pattern REGLIST = Pattern.compile("\\[[^\\]]*\\]");
	private static final Pattern NUM = Pattern.compile("(?<![a-z0-9_])-?(0x[0-9a-f]+|[0-9]+)(?![0-9a-z_])");
	private static final Pattern DREG = Pattern.compile("(?<![a-z0-9_])d[0-3](?![0-9a-z_])");
	private static final Pattern AREG = Pattern.compile("(?<![a-z0-9_])a[0-3](?![0-9a-z_])");

	static String form(Instruction i) {
		String s = i.toString().toLowerCase().replace(" ", "\u0001");
		int sp = s.indexOf('\u0001');
		String mnem = sp < 0 ? s : s.substring(0, sp);
		String ops = sp < 0 ? "" : s.substring(sp + 1).replace("\u0001", "");
		ops = REGLIST.matcher(ops).replaceAll("[regs]");
		ops = NUM.matcher(ops).replaceAll("#");
		ops = DREG.matcher(ops).replaceAll("Dn");
		ops = AREG.matcher(ops).replaceAll("An");
		return ops.isEmpty() ? mnem : mnem + " " + ops;
	}

	@Override
	protected void run() throws Exception {
		String[] args = getScriptArgs();
		if (args.length != 1 && args.length != 3) {
			throw new IllegalArgumentException("usage: MN103Forms <out.tsv> [<offsets-file> <insns.tsv>]");
		}
		Map<String, Long> hist = new HashMap<>();
		AddressSet scope = new AddressSet();
		if (args.length == 3) {
			Address base = currentProgram.getMinAddress();
			AddressSet starts = new AddressSet();
			for (String line : Files.readAllLines(Path.of(args[1]))) {
				if (!line.isBlank()) {
					starts.add(base.add(Long.parseLong(line.trim())));
				}
			}
			new DisassembleCommand(starts, null, false).applyTo(currentProgram, monitor);
			for (Address a : starts.getAddresses(true)) {
				if (currentProgram.getListing().getInstructionAt(a) == null) {
					hist.merge("UNDECODABLE\t0", 1L, Long::sum);
				}
			}
			scope = starts;
			try (PrintWriter out = new PrintWriter(args[2])) {
				InstructionIterator it = currentProgram.getListing().getInstructions(starts, true);
				while (it.hasNext()) {
					Instruction i = it.next();
					out.printf("%d\t%d\t%s\t%s%n", i.getMinAddress().subtract(base), i.getLength(), i, form(i));
				}
			}
		}
		else {
			for (MemoryBlock b : currentProgram.getMemory().getBlocks()) {
				if (b.isExecute() && b.isInitialized()) {
					scope.add(b.getStart(), b.getEnd());
				}
			}
		}
		InstructionIterator it = currentProgram.getListing().getInstructions(scope, true);
		while (it.hasNext() && !monitor.isCancelled()) {
			Instruction i = it.next();
			String key = form(i) + "\t" + i.getLength();
			hist.merge(key, 1L, Long::sum);
		}
		try (PrintWriter out = new PrintWriter(args[0])) {
			for (Map.Entry<String, Long> kv : new TreeMap<>(hist).entrySet()) {
				out.printf("%s\t%d%n", kv.getKey(), kv.getValue());
			}
		}
		println("MN103Forms: " + hist.size() + " forms written to " + args[0]);
	}
}
