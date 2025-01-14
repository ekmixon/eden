Check for contents we should refuse to export to git repositories (or
at least warn).

Load commonly used test logic
  $ . "$TESTDIR/hggit/testutil"

  $ hg init hg
  $ cd hg
  $ mkdir -p .git/hooks
  $ cat > .git/hooks/post-update <<EOF
  > #!/bin/sh
  > echo pwned
  > EOF

#if fsmonitor
  $ hg addremove
#else
  $ hg addremove
  adding .git/hooks/post-update
  $ hg ci -m "we should refuse to export this"
  $ hg book master
  $ hg gexport
  abort: Refusing to export likely-dangerous path '.git/hooks/post-update'
  (If you need to continue, read about CVE-2014-9390 and then set '[git] blockdotgit = false' in your hgrc.)
  [255]
#endif
  $ cd ..

  $ rm -rf hg
  $ hg init hg
  $ cd hg
  $ mkdir -p nested/.git/hooks/
  $ cat > nested/.git/hooks/post-update <<EOF
  > #!/bin/sh
  > echo pwnd
  > EOF
  $ chmod +x nested/.git/hooks/post-update
  $ hg addremove
  adding nested/.git/hooks/post-update
  $ hg ci -m "also refuse to export this"
  $ hg book master
  $ hg gexport
  abort: Refusing to export likely-dangerous path 'nested/.git/hooks/post-update'
  (If you need to continue, read about CVE-2014-9390 and then set '[git] blockdotgit = false' in your hgrc.)
  [255]
We can override if needed:
  $ hg --config git.blockdotgit=false gexport
  warning: path 'nested/.git/hooks/post-update' contains a dangerous path component.
  It may not be legal to check out in Git.
  It may also be rejected by some git server configurations.
  $ cd ..
  $ git clone hg/.hg/git git
  Cloning into 'git'...
  done.
  error: invalid path 'nested/.git/hooks/post-update'
  fatal: unable to checkout working tree
  warning: Clone succeeded, but checkout failed.
  You can inspect what was checked out with 'git status'
  and retry with 'git restore --source=HEAD :/'
  
  [128]

Now check something that case-folds to .git, which might let you own
Mac users:

  $ cd ..
  $ rm -rf hg
  $ hg init hg
  $ cd hg
  $ mkdir -p .GIT/hooks/
  $ cat > .GIT/hooks/post-checkout <<EOF
  > #!/bin/sh
  > echo pwnd
  > EOF
  $ chmod +x .GIT/hooks/post-checkout
  $ hg addremove
  adding .GIT/hooks/post-checkout
  $ hg ci -m "also refuse to export this"
  $ hg book master
  $ hg gexport
  $ cd ..

And the NTFS case:
  $ cd ..
  $ rm -rf hg
  $ hg init hg
  $ cd hg
  $ mkdir -p GIT~1/hooks/
  $ cat > GIT~1/hooks/post-checkout <<EOF
  > #!/bin/sh
  > echo pwnd
  > EOF
  $ chmod +x GIT~1/hooks/post-checkout
  $ hg addremove
  adding GIT~1/hooks/post-checkout
  $ hg ci -m "also refuse to export this"
  $ hg book master
  $ hg gexport
  abort: Refusing to export likely-dangerous path 'GIT~1/hooks/post-checkout'
  (If you need to continue, read about CVE-2014-9390 and then set '[git] blockdotgit = false' in your hgrc.)
  [255]
  $ cd ..

Now check a Git repository containing a Mercurial repository, which
you can't check out.

  $ rm -rf hg git nested
  $ git init -q git
  $ hg init nested
  $ mv nested git
  $ cd git
  $ git add nested
  $ fn_git_commit -m 'add a Mercurial repository'
  $ cd ..
  $ hg clone git hg
  importing git objects into hg
  abort: Refusing to import problematic path 'nested/.hg/00changelog.i'
  (Mercurial cannot check out paths inside nested repositories; if you need to continue, then set '[git] blockdothg = false' in your hgrc.)
  [255]
  $ hg clone --config git.blockdothg=false git hg
  importing git objects into hg
  warning: path 'nested/.hg/00changelog.i' is within a nested repository, which Mercurial cannot check out.
  warning: path 'nested/.hg/hgrc.dynamic' is within a nested repository, which Mercurial cannot check out.
  warning: path 'nested/.hg/reponame' is within a nested repository, which Mercurial cannot check out.
  warning: path 'nested/.hg/requires' is within a nested repository, which Mercurial cannot check out.
  warning: path 'nested/.hg/store/metalog/blobs/index2-id' is within a nested repository, which Mercurial cannot check out.
  warning: path 'nested/.hg/store/metalog/blobs/log' is within a nested repository, which Mercurial cannot check out.
  warning: path 'nested/.hg/store/metalog/blobs/meta' is within a nested repository, which Mercurial cannot check out.
  warning: path 'nested/.hg/store/metalog/roots/index2-reverse' is within a nested repository, which Mercurial cannot check out.
  warning: path 'nested/.hg/store/metalog/roots/log' is within a nested repository, which Mercurial cannot check out.
  warning: path 'nested/.hg/store/metalog/roots/meta' is within a nested repository, which Mercurial cannot check out.
  warning: path 'nested/.hg/store/requires' is within a nested repository, which Mercurial cannot check out.
  updating to branch default
  abort: path 'nested/.hg/00changelog.i' is inside nested repo 'nested'
  [255]
  $ cd ..
