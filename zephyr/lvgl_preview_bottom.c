/*
 * lvgl_preview_bottom.c - host-side half of the preview transport.
 *
 * SPDX-License-Identifier: Apache-2.0
 *
 * Built in the native simulator runner context: host libC, host headers, host
 * syscalls. This is the only file in the module that is allowed to touch the
 * filesystem directly.
 */

#include "lvgl_preview_bottom.h"

#include <stdbool.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include "lvgl_preview_proto.h"

struct lvp_map {
	void *base;
	size_t size;
};

static int open_and_size(const char *path, size_t size, int *fd_out, bool *created_out)
{
	struct stat st;
	int fd;
	bool created = false;

	fd = open(path, O_RDWR);
	if (fd < 0) {
		fd = open(path, O_RDWR | O_CREAT, 0644);
		if (fd < 0) {
			perror("lvgl-preview: open");
			return -1;
		}
		created = true;
	}

	if (fstat(fd, &st) < 0) {
		perror("lvgl-preview: fstat");
		close(fd);
		return -1;
	}

	/*
	 * Grow only. Truncating a mapping that a viewer already has open would
	 * discard its mapping and hand the device a fresh, zeroed file.
	 */
	if ((size_t)st.st_size < size) {
		if (ftruncate(fd, (off_t)size) < 0) {
			perror("lvgl-preview: ftruncate");
			close(fd);
			return -1;
		}
		created = true;
	}

	*fd_out = fd;
	if (created_out != NULL) {
		*created_out = created;
	}

	return 0;
}

int lvp_host_map_frame(const char *path, uint32_t pixel_capacity, void **handle, void **header,
		       void **pixels)
{
	struct lvp_map *map;
	size_t size;
	void *base;
	int fd;

	if ((path == NULL) || (handle == NULL) || (header == NULL) || (pixels == NULL)) {
		return -1;
	}

	size = sizeof(struct lvp_frame_header) + (size_t)pixel_capacity;

	if (open_and_size(path, size, &fd, NULL) != 0) {
		return -1;
	}

	base = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	close(fd);
	if (base == MAP_FAILED) {
		perror("lvgl-preview: mmap frame");
		return -1;
	}

	map = malloc(sizeof(*map));
	if (map == NULL) {
		munmap(base, size);
		return -1;
	}

	map->base = base;
	map->size = size;

	*handle = map;
	*header = base;
	*pixels = (uint8_t *)base + sizeof(struct lvp_frame_header);

	return 0;
}

void lvp_host_unmap_frame(void *handle)
{
	struct lvp_map *map = handle;

	if (map == NULL) {
		return;
	}

	munmap(map->base, map->size);
	free(map);
}

int lvp_host_map_input(const char *path, void **handle, void **ring)
{
	struct lvp_map *map;
	size_t size = LVP_INPUT_FILE_SIZE;
	void *base;
	int fd;

	if ((path == NULL) || (handle == NULL) || (ring == NULL)) {
		return -1;
	}

	if (open_and_size(path, size, &fd, NULL) != 0) {
		return -1;
	}

	base = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	close(fd);
	if (base == MAP_FAILED) {
		perror("lvgl-preview: mmap input");
		return -1;
	}

	map = malloc(sizeof(*map));
	if (map == NULL) {
		munmap(base, size);
		return -1;
	}

	map->base = base;
	map->size = size;

	*handle = map;
	*ring = base;

	return 0;
}

void lvp_host_unmap_input(void *handle)
{
	struct lvp_map *map = handle;

	if (map == NULL) {
		return;
	}

	munmap(map->base, map->size);
	free(map);
}

int lvp_host_write_manifest(const char *path, const char *text, uint32_t length)
{
	FILE *f;

	if ((path == NULL) || (text == NULL)) {
		return -1;
	}

	f = fopen(path, "wb");
	if (f == NULL) {
		perror("lvgl-preview: fopen manifest");
		return -1;
	}

	if (fwrite(text, 1, length, f) != length) {
		fclose(f);
		return -1;
	}

	if (fclose(f) != 0) {
		return -1;
	}

	return 0;
}
