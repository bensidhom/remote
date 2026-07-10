import React, { useState } from 'react';
import { ShieldCheck } from 'lucide-react';
import { acceptLicense } from '../lib/access';
import { Button, GradientText, Aurora } from './ui';

/** Full-screen license/terms acknowledgement shown once before the app loads. */
const LicenseGate: React.FC<{ onAccept: () => void }> = ({ onAccept }) => {
  const [agreed, setAgreed] = useState(false);

  const proceed = () => {
    if (!agreed) return;
    acceptLicense();
    onAccept();
  };

  return (
    <div className="fixed inset-0 z-[10000] flex items-center justify-center p-4 bg-slate-950">
      <Aurora intensity="soft" />
      <div className="relative w-full max-w-2xl glass-strong rounded-3xl border border-slate-700/70 shadow-2xl p-8 animate-scale-in">
        <div className="flex items-center gap-3 mb-5">
          <span className="grid place-items-center w-12 h-12 rounded-2xl bg-brand-500/15 text-brand-300">
            <ShieldCheck className="w-6 h-6" />
          </span>
          <div>
            <GradientText as="h1" className="text-2xl tracking-tight">BridgeGuard — License & Terms</GradientText>
            <p className="text-sm text-slate-400">Please review and accept before continuing.</p>
          </div>
        </div>

        <div className="ui-scroll max-h-[42vh] overflow-auto rounded-2xl bg-slate-900/60 border border-slate-700/60 p-5 text-sm leading-relaxed text-slate-300 space-y-3">
          <p className="font-semibold text-slate-200">BridgeGuard Software License & Data-Use Agreement</p>
          <p>This software ("BridgeGuard") is provided for <strong>authorized internal use only</strong> by
          designated New Jersey Department of Transportation (NJDOT) personnel and approved research
          collaborators. By accepting, you agree to the following terms:</p>
          <p><strong>1. Permitted use.</strong> You may use BridgeGuard solely to load, convert, view,
          analyze, and manage New Jersey bridge inventory and inspection data for official or approved
          research purposes.</p>
          <p><strong>2. No redistribution.</strong> You may not copy, distribute, sublicense, sell, or
          publicly disclose the software, its source code, or its underlying models without prior
          written authorization.</p>
          <p><strong>3. Data handling.</strong> Bridge inspection and inventory data may be sensitive
          infrastructure information. You agree to handle all data in accordance with NJDOT data-handling
          policies and applicable law, and to keep it on authorized devices only.</p>
          <p><strong>4. AI-generated content.</strong> Forecasts, cost estimates, and AI assistant
          ("Bridgette") responses are advisory and may be inaccurate. They are not a substitute for a
          licensed engineer's professional judgment or an official inspection.</p>
          <p><strong>5. No warranty.</strong> The software is provided "as is", without warranty of any
          kind. The authors and NJDOT are not liable for any damages arising from its use.</p>
          <p><strong>6. Access.</strong> Access is restricted to authorized accounts. Sharing credentials
          or circumventing access controls is prohibited.</p>
          <p className="text-slate-400">By checking the box below you confirm you are an authorized user and
          accept these terms.</p>
        </div>

        <label className="flex items-center gap-3 mt-5 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={agreed}
            onChange={(e) => setAgreed(e.target.checked)}
            className="w-5 h-5 rounded accent-brand-500"
          />
          <span className="text-sm text-slate-200">I have read and agree to the License & Terms.</span>
        </label>

        <div className="flex justify-end mt-6">
          <Button variant="primary" size="lg" disabled={!agreed} onClick={proceed}>
            Agree & Continue
          </Button>
        </div>
      </div>
    </div>
  );
};

export default LicenseGate;
