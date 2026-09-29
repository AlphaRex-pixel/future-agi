import PropTypes from "prop-types";
import RunsSummary from "./summary/RunsSummary";

// The Runs tab: every run of the environment as one summary — the eval-score
// trend graph over the runs table. The tab only shows once the environment has
// a run, so there is nothing to render before the list arrives.
export default function RunsPanel({ env, envState, runs, onOpenRun, onGo }) {
  if (runs.length === 0) return null;
  return (
    <RunsSummary
      env={env}
      envState={envState}
      onOpenRun={onOpenRun}
      onGo={onGo}
    />
  );
}

RunsPanel.propTypes = {
  env: PropTypes.shape({
    id: PropTypes.string,
    name: PropTypes.string,
    surface: PropTypes.string,
  }).isRequired,
  envState: PropTypes.shape({
    scenarios: PropTypes.arrayOf(PropTypes.object),
    evals: PropTypes.arrayOf(
      PropTypes.oneOfType([
        PropTypes.string,
        PropTypes.shape({ id: PropTypes.string, name: PropTypes.string }),
      ]),
    ),
  }).isRequired,
  runs: PropTypes.arrayOf(PropTypes.shape({ id: PropTypes.string, status: PropTypes.string }))
    .isRequired,
  onOpenRun: PropTypes.func,
  onGo: PropTypes.func,
};
