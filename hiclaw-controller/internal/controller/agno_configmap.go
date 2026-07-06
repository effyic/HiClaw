package controller

import (
	"context"
	"fmt"

	"github.com/hiclaw/hiclaw-controller/internal/executor"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

// EnsureAgnoAgentSpecConfigMap upserts a ConfigMap with AgentSpec files and
// returns the ConfigMap name for Pod mounting.
func EnsureAgnoAgentSpecConfigMap(
	ctx context.Context,
	c client.Client,
	namespace, workerName string,
	data map[string]string,
	owner metav1.Object,
) (string, error) {
	if c == nil || namespace == "" || workerName == "" {
		return "", fmt.Errorf("k8s client, namespace, and worker name are required")
	}
	if len(data) == 0 {
		return "", fmt.Errorf("agentspec data is empty for worker %s", workerName)
	}

	name := executor.AgnoAgentSpecConfigMapName(workerName)
	cm := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: namespace,
			Labels: map[string]string{
				"hiclaw.io/runtime": "agno",
				"hiclaw.io/worker":  workerName,
			},
		},
		Data: data,
	}
	if owner != nil {
		if err := controllerutil.SetControllerReference(owner, cm, c.Scheme()); err != nil {
			return "", fmt.Errorf("set owner on agentspec ConfigMap: %w", err)
		}
	}

	existing := &corev1.ConfigMap{}
	err := c.Get(ctx, client.ObjectKey{Namespace: namespace, Name: name}, existing)
	switch {
	case apierrors.IsNotFound(err):
		if err := c.Create(ctx, cm); err != nil {
			return "", fmt.Errorf("create agentspec ConfigMap: %w", err)
		}
	case err != nil:
		return "", fmt.Errorf("get agentspec ConfigMap: %w", err)
	default:
		existing.Data = data
		existing.Labels = cm.Labels
		if err := c.Update(ctx, existing); err != nil {
			return "", fmt.Errorf("update agentspec ConfigMap: %w", err)
		}
	}
	return name, nil
}
