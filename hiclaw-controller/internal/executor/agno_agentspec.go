package executor

import (
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

const maxAgentSpecFileBytes = 512 * 1024

// PackAgentSpecDir walks an extracted AgentSpec directory and returns
// ConfigMap data keys (relative POSIX paths) → file contents.
func PackAgentSpecDir(root string) (map[string]string, error) {
	rootAbs, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	info, err := os.Stat(rootAbs)
	if err != nil {
		return nil, fmt.Errorf("agentspec dir %s: %w", rootAbs, err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("agentspec path %s is not a directory", rootAbs)
	}

	out := make(map[string]string)
	err = filepath.WalkDir(rootAbs, func(path string, d fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if d.IsDir() {
			return nil
		}
		rel, err := filepath.Rel(rootAbs, path)
		if err != nil {
			return err
		}
		key := filepath.ToSlash(rel)
		if strings.HasPrefix(key, "..") {
			return fmt.Errorf("unsafe agentspec path %q", key)
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		if len(data) > maxAgentSpecFileBytes {
			return fmt.Errorf("agentspec file %q exceeds %d bytes", key, maxAgentSpecFileBytes)
		}
		out[key] = string(data)
		return nil
	})
	if err != nil {
		return nil, err
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("no files found under agentspec dir %s", rootAbs)
	}
	return out, nil
}

// AgnoAgentSpecConfigMapName returns the ConfigMap name for a worker's AgentSpec.
func AgnoAgentSpecConfigMapName(workerName string) string {
	return "agno-agentspec-" + workerName
}
