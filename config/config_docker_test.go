package config

import "testing"

// TestDockerNetworkConfiguration_IsContainerNetworkMode tests the IsContainerNetworkMode
// method to ensure it correctly identifies when the network mode is set to share another
// container's network namespace (i.e., "container:<name>" format).
func TestDockerNetworkConfiguration_IsContainerNetworkMode(t *testing.T) {
	tests := []struct {
		name     string
		mode     string
		expected bool
	}{
		{"container mode with name", "container:caddy", true},
		{"container mode with different name", "container:some-vpn-container", true},
		{"container mode empty name", "container:", false}, // Docker rejects "container:" without a name
		{"default pelican network", "pelican_nw", false},
		{"bridge network", "bridge", false},
		{"host network", "host", false},
		{"empty string", "", false},
		{"partial match", "containers", false}, // Should not match without colon
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			c := DockerNetworkConfiguration{Mode: tt.mode}
			if got := c.IsContainerNetworkMode(); got != tt.expected {
				t.Errorf("IsContainerNetworkMode() = %v, want %v for mode %q", got, tt.expected, tt.mode)
			}
		})
	}
}

func TestDockerRegistryCredentialsForImage(t *testing.T) {
	cfg := DockerConfiguration{
		Registries: map[string]RegistryConfiguration{
			"registry.example.com": {
				Username: "registry",
				Password: "secret",
			},
			"registry.example.com/team": {
				Username: "team",
				Password: "secret",
			},
			"registry.example.com:5000": {
				Username: "port",
				Password: "secret",
			},
			"https://index.docker.io/v1/": {
				Username: "docker",
				Password: "secret",
			},
		},
	}

	tests := []struct {
		name     string
		image    string
		username string
	}{
		{
			name:     "registry domain",
			image:    "registry.example.com/project/image:latest",
			username: "registry",
		},
		{
			name:     "registry with port",
			image:    "registry.example.com:5000/project/image:latest",
			username: "port",
		},
		{
			name:     "registry path",
			image:    "registry.example.com/team/image:latest",
			username: "team",
		},
		{
			name:  "registry prefix is not domain",
			image: "registry.example.com.evil/project/image:latest",
		},
		{
			name:     "registry path prefix falls back to domain",
			image:    "registry.example.com/team-evil/image:latest",
			username: "registry",
		},
		{
			name:     "legacy docker hub registry",
			image:    "docker.io/library/busybox:latest",
			username: "docker",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			_, registry := cfg.RegistryCredentialsForImage(tt.image)
			if tt.username == "" {
				if registry != nil {
					t.Fatalf("expected no registry credentials, got username %q", registry.Username)
				}

				return
			}

			if registry == nil {
				t.Fatalf("expected registry credentials for %q", tt.image)
			}

			if registry.Username != tt.username {
				t.Fatalf("expected username %q, got %q", tt.username, registry.Username)
			}
		})
	}
}

func TestDockerRegistryPathCredentialsDoNotMatchSiblingPath(t *testing.T) {
	cfg := DockerConfiguration{
		Registries: map[string]RegistryConfiguration{
			"registry.example.com/team": {
				Username: "team",
				Password: "secret",
			},
		},
	}

	_, registry := cfg.RegistryCredentialsForImage("registry.example.com/team-evil/image:latest")
	if registry != nil {
		t.Fatalf("expected no registry credentials, got username %q", registry.Username)
	}
}
