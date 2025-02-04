#chg-compatible
#require execbit

  $ disable treemanifest

  $ enable convert
  $ setconfig convert.hg.saverev=False

  $ hg init orig
  $ cd orig
  $ echo foo > foo
  $ echo bar > bar
  $ hg ci -qAm 'add foo bar' -d '0 0'
  $ echo >> foo
  $ hg ci -m 'change foo' -d '1 0'
  $ hg up -qC 'desc(add)'
  $ hg copy --after --force foo bar
  $ hg copy foo baz
  $ hg ci -m 'make bar and baz copies of foo' -d '2 0'

Test that template can print all file copies (issue4362)
  $ hg log -r . --template "{file_copies % ' File: {file_copy}\n'}"
   File: bar (foo)
   File: baz (foo)

  $ hg bookmark premerge1
  $ hg merge -r 'desc(change)'
  merging baz and foo to baz
  1 files updated, 1 files merged, 0 files removed, 0 files unresolved
  (branch merge, don't forget to commit)
  $ hg ci -m 'merge local copy' -d '3 0'
  $ hg up -C 'desc(change)'
  1 files updated, 0 files merged, 1 files removed, 0 files unresolved
  (leaving bookmark premerge1)
  $ hg bookmark premerge2
  $ hg merge 'desc(make)'
  merging foo and baz to baz
  1 files updated, 1 files merged, 0 files removed, 0 files unresolved
  (branch merge, don't forget to commit)
  $ hg ci -m 'merge remote copy' -d '4 0'

  $ chmod +x baz
  $ hg ci -m 'mark baz executable' -d '5 0'
  $ cd ..
  $ hg convert --datesort orig new 2>&1 | grep -v 'subversion python bindings could not be loaded'
  initializing destination new repository
  scanning source...
  sorting...
  converting...
  5 add foo bar
  4 change foo
  3 make bar and baz copies of foo
  2 merge local copy
  1 merge remote copy
  0 mark baz executable
  updating bookmarks
  $ cd new
  $ hg out ../orig
  comparing with ../orig
  searching for changes
  no changes found
  [1]
  $ hg bookmarks
     premerge1                 973ef48a98a4
     premerge2                 13d9b87cf8f8

Test that redoing a convert results in an identical graph
  $ cd ../
  $ rm new/.hg/shamap
  $ hg convert --datesort orig new 2>&1 | grep -v 'subversion python bindings could not be loaded'
  scanning source...
  sorting...
  converting...
  5 add foo bar
  4 change foo
  3 make bar and baz copies of foo
  2 merge local copy
  1 merge remote copy
  0 mark baz executable
  updating bookmarks
  $ hg -R new log -G -T '{desc}'
  o  mark baz executable
  │
  o    merge remote copy
  ├─╮
  │ │ o  merge local copy
  ╭─┬─╯
  │ o  make bar and baz copies of foo
  │ │
  o │  change foo
  ├─╯
  o  add foo bar
  

check shamap LF and CRLF handling

  $ cat > rewrite.py <<EOF
  > import sys
  > # Interlace LF and CRLF
  > lines = [(l.rstrip() + ((i % 2) and b'\n' or b'\r\n'))
  >          for i, l in enumerate(open(sys.argv[1], 'rb'))]
  > _ = open(sys.argv[1], 'wb').write(b''.join(lines))
  > EOF
  $ $PYTHON rewrite.py new/.hg/shamap
  $ cd orig
  $ hg up -qC 'desc(change)'
  $ echo foo >> foo
  $ hg ci -qm 'change foo again'
  $ hg up -qC 'desc(make)'
  $ echo foo >> foo
  $ hg ci -qm 'change foo again again'
  $ cd ..
  $ hg convert --datesort orig new 2>&1 | grep -v 'subversion python bindings could not be loaded'
  scanning source...
  sorting...
  converting...
  1 change foo again again
  0 change foo again
  updating bookmarks

init broken repository

  $ hg init broken
  $ cd broken
  $ echo a >> a
  $ echo b >> b
  $ hg ci -qAm init
  $ echo a >> a
  $ echo b >> b
  $ hg copy b c
  $ hg ci -qAm changeall
  $ hg up -qC 'desc(init)'
  $ echo bc >> b
  $ hg ci -m changebagain
  $ HGMERGE=internal:local hg -q merge
  $ hg ci -m merge
  $ hg mv b d
  $ hg ci -m moveb

break it

  $ rm .hg/store/data/b.*
  $ cd ..
  $ hg --config convert.hg.ignoreerrors=True convert broken fixed
  initializing destination fixed repository
  scanning source...
  sorting...
  converting...
  4 init
  ignoring: data/b.i@1e88685f5dde: no match found
  3 changeall
  2 changebagain
  1 merge
  0 moveb
  $ hg -R fixed verify
  warning: verify does not actually check anything in this repo

manifest -r 0

  $ hg -R fixed manifest -r 'desc(init)'
  a

manifest -r tip

  $ hg -R fixed manifest -r tip
  a
  c
  d
