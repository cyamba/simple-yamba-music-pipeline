from run_batch import musescore_convert


def fake_mscore(tmp_path, body):
    """A stand-in for MuseScore's command line, called as: mscore -o <dst> <src>."""
    script = tmp_path / "mscore"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return str(script)


def test_an_abort_after_writing_the_output_counts_as_done(tmp_path):
    # What MuseScore 4.7.5 does on macOS 27 about half the time: a complete file, then SIGABRT.
    mscore = fake_mscore(tmp_path, 'echo "%PDF" > "$2"\nkill -ABRT $$\n')
    assert musescore_convert(mscore, tmp_path / "in.musicxml", tmp_path / "out.pdf") == "yes"


def test_an_abort_without_output_is_still_a_failure(tmp_path):
    mscore = fake_mscore(tmp_path, "kill -ABRT $$\n")
    assert musescore_convert(mscore, tmp_path / "in.musicxml", tmp_path / "out.pdf").startswith("no (exit -6")
