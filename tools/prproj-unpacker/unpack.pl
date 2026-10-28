use strict;
use warnings;

use IO::Uncompress::Gunzip qw(gunzip $GunzipError);
use File::Basename qw(fileparse);
use File::Spec;

my $src = $ARGV[0]
    or die "Usage: perl $0 <input.prproj> [output.xml]\n";

my $dst = $ARGV[1];
if (!defined $dst) {
    $dst = $src;
    $dst .= '.xml' unless $dst =~ s/\.[^.\\\/]+$/.xml/;
}

open my $fh, '<:raw', $src
    or die "Failed to open $src: $!\n";

read($fh, my $header, 2);
close $fh;

if (defined $header && $header eq "\x1f\x8b") {
    my $unpacked = '';
    gunzip $src => \$unpacked
        or die "GZIP decompression failed: $GunzipError\n";

    open my $out, '>:raw', $dst
        or die "Failed to create $dst: $!\n";

    print {$out} $unpacked
        or die "Failed to write $dst: $!\n";

    close $out
        or die "Failed to close $dst: $!\n";

    print "Source file size: ", (-s $src), "\n";
    print "Unpacked size: ", length($unpacked), "\n";
    print "First 100 bytes: ",
          unpack('H*', substr($unpacked, 0, 100)), "\n";
    print "Unpacked file: $dst\n";
} else {
    print "GZIP signature not found.\n";
}