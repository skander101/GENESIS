class Node:
    def __init__(self, data):
        self.data = data
        self.next = None


class LinkedList:
    def __init__(self):
        self.head = None

    def append(self, data):
        """Add a node to the end of the list."""
        new_node = Node(data)
        if not self.head:
            self.head = new_node
            return
        current = self.head
        while current.next:
            current = current.next
        current.next = new_node

    def display(self):
        """Print the linked list."""
        elements = []
        current = self.head
        while current:
            elements.append(str(current.data))
            current = current.next
        print(" -> ".join(elements) + " -> None")

    def reverse(self):
        """
        Reverse the linked list in-place.
        
        Algorithm:
        1. Use three pointers: prev, current, and next_node.
        2. For each node, reverse its 'next' pointer to point to the previous node.
        3. Move all three pointers one step forward.
        4. After the loop, update head to point to the last node (which is now the first).
        
        Time Complexity:  O(n) — visits each node once.
        Space Complexity: O(1) — only uses three extra pointers.
        """
        prev = None
        current = self.head

        while current:
            next_node = current.next   # Save the next node
            current.next = prev        # Reverse the link
            prev = current             # Move prev forward
            current = next_node        # Move current forward

        self.head = prev  # Update head to the new first node


# --- Example Usage ---
if __name__ == "__main__":
    ll = LinkedList()
    for val in [1, 2, 3, 4, 5]:
        ll.append(val)

    print("Original list:")
    ll.display()  # 1 -> 2 -> 3 -> 4 -> 5 -> None

    ll.reverse()

    print("Reversed list:")
    ll.display()  # 5 -> 4 -> 3 -> 2 -> 1 -> None
