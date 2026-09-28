import * as React from 'react';
import './poc.css';

const PocPage: React.FC = () => {
  const [clicks, setClicks] = React.useState(0);

  return (
    <div className="poc-plugin__page">
      <h1 className="poc-plugin__title">POC Console Plugin</h1>
      <p className="poc-plugin__body">
        This page is not part of the core OpenShift console image. It was loaded
        at runtime from a customer <code>ConsolePlugin</code>, the same
        mechanism ACM uses.
      </p>
      <p className="poc-plugin__body">
        If you can see this under <strong>Home → POC Plugin</strong>, the proof
        of concept worked.
      </p>
      <button
        type="button"
        className="poc-plugin__button"
        onClick={() => setClicks((n) => n + 1)}
      >
        Clicked {clicks} {clicks === 1 ? 'time' : 'times'}
      </button>
    </div>
  );
};

export default PocPage;
