import React, { useState } from 'react';
import { Lock, LogIn, Mail } from 'lucide-react';
import { signIn, type AuthUser } from '../lib/access';
import { Button, GradientText, Aurora } from './ui';

/** Full-screen email/password login. */
const EmailLoginGate: React.FC<{ onSignedIn: (user: AuthUser) => void }> = ({ onSignedIn }) => {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const submit = async () => {
    const e = email.trim().toLowerCase();
    if (!e) { setError('Enter your email address.'); return; }
    if (!password) { setError('Enter the BridgeGuard password.'); return; }
    setSubmitting(true);
    try {
      const user = await signIn(e, password);
      onSignedIn(user);
    } catch (err: any) {
      setError(err?.response?.data?.detail || 'Unable to sign in. Please try again.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[10000] flex items-center justify-center p-4 bg-slate-950">
      <Aurora intensity="soft" />
      <div className="relative w-full max-w-md glass-strong rounded-3xl border border-slate-700/70 shadow-2xl p-8 animate-scale-in">
        <div className="flex flex-col items-center text-center mb-6">
          <span className="grid place-items-center w-14 h-14 rounded-2xl bg-brand-gradient shadow-glow text-white mb-3">
            <LogIn className="w-7 h-7" />
          </span>
          <GradientText as="h1" className="text-2xl tracking-tight">Sign in to BridgeGuard</GradientText>
          <p className="text-sm text-slate-400 mt-1">Authorized accounts only.</p>
        </div>

        <label className="block">
          <span className="text-xs font-medium text-slate-400">Work email</span>
          <div className="mt-1 flex items-center gap-2 rounded-xl border border-slate-700 bg-slate-900/70 px-3">
            <Mail className="w-4 h-4 text-slate-400" />
            <input
              type="email"
              autoFocus
              value={email}
              onChange={(e) => { setEmail(e.target.value); setError(''); }}
              placeholder="you@dot.nj.gov"
              className="flex-1 bg-transparent py-3 outline-none text-slate-100 placeholder:text-slate-500"
            />
          </div>
        </label>

        <label className="block mt-4">
          <span className="text-xs font-medium text-slate-400">Password</span>
          <div className="mt-1 flex items-center gap-2 rounded-xl border border-slate-700 bg-slate-900/70 px-3">
            <Lock className="w-4 h-4 text-slate-400" />
            <input
              type="password"
              value={password}
              onChange={(e) => { setPassword(e.target.value); setError(''); }}
              onKeyDown={(e) => { if (e.key === 'Enter') void submit(); }}
              placeholder="BridgeGuard password"
              className="flex-1 bg-transparent py-3 outline-none text-slate-100 placeholder:text-slate-500"
            />
          </div>
        </label>
        {error && <p className="text-sm text-red-400 mt-2">{error}</p>}

        <Button variant="primary" size="lg" fullWidth className="mt-6" onClick={() => void submit()} disabled={submitting}>
          {submitting ? 'Signing in…' : 'Continue'}
        </Button>
      </div>
    </div>
  );
};

export default EmailLoginGate;
