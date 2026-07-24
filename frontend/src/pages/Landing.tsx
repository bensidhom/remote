import React from 'react';
import { ArrowUpRight, LockKeyhole } from 'lucide-react';

const Landing: React.FC = () => (
  <main className="public-landing">
    <h1 className="sr-only">BridgeGuard</h1>

    <div className="public-landing__center">
      <img
        className="public-landing__logo"
        src="/bridgeguard.png"
        alt="BridgeGuard"
        width="1024"
        height="1024"
      />

      <a className="public-landing__access" href="/app">
        <LockKeyhole aria-hidden="true" />
        <span>Open workspace</span>
        <ArrowUpRight aria-hidden="true" />
      </a>
    </div>
  </main>
);

export default Landing;
